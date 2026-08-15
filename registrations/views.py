from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, FileResponse, Http404
from django.db import transaction
from django.db.models import Q
from django.conf import settings
from django.core.files.storage import FileSystemStorage
from registrations.models import StudentApplication, UploadedDocument, SUBMITTED_STATUS_CODES
from registrations.forms import StudentApplicationForm
from dashboard.form_config_service import FormConfigurationService
from masterdata.models import (
    Country, State, District, Religion, Qualification, Program, Occupation, 
    Community, MaritalStatus, ApplicationStatus, YearOfStudy, ExServiceStatus,
    TrainingPartner
)
from utilities.application_number import generate_application_number
from utilities.image_optimizer import process_and_optimize_image
from utilities.pdf_optimizer import compress_pdf
from utilities.acknowledgement_generator import generate_acknowledgement_files
from utilities.folder_manager import get_student_folder_name
from django.contrib import messages
import mimetypes
import os
import shutil
import logging
import traceback
import io
import zipfile
from django.utils.crypto import get_random_string

logger = logging.getLogger(__name__)

def resolve_fk(model_class, value):
    if not value:
        return None
    try:
        return model_class.objects.filter(id=value).first()
    except (ValueError, TypeError):
        return None

def get_name(model_class, id_val):
    if not id_val:
        return "-"
    obj = model_class.objects.filter(id=id_val).first()
    return str(obj) if obj else "-"

def app_to_form_data(app):
    """Convert a StudentApplication model instance into form_data dict for pre-filling."""
    d = {}
    simple_fields = [
        'full_name', 'father_name', 'mother_name', 'dob', 'gender',
        'nationality', 'physically_handicapped', 'annual_income',
        'mobile_number', 'alternative_mobile', 'email', 'communication_address',
        'pincode', 'area_village_name', 'institution', 'hall_ticket_number',
        'registration_number', 'applying_qualification', 'custom_stream',
        'mode_of_qualification', 'completion_status', 'percentage',
        'year_of_passing', 'batch_code', 'aadhaar_number', 'abc_id',
        'distinguishing_mark',
    ]
    for f in simple_fields:
        d[f] = getattr(app, f, '')
    d['consent_given'] = bool(app.consent_given)
    fk_fields = {
        'country': 'country_id', 'state': 'state_id', 'district': 'district_id',
        'religion': 'religion_id', 'marital_status': 'marital_status_id',
        'community': 'community_id', 'occupation': 'occupation_id',
        'qualification': 'qualification_id', 'program_opting': 'program_opting_id',
        'training_partner': 'training_partner_id', 'ex_serviceman': 'ex_serviceman_id',
    }
    for key, attr in fk_fields.items():
        v = getattr(app, attr, None)
        d[key] = str(v) if v else ''
    return d

def populate_app_from_data(app, cleaned):
    """Map validated form cleaned_data onto a StudentApplication instance."""
    app.full_name = cleaned.get('full_name', '')
    app.father_name = cleaned.get('father_name', '')
    app.mother_name = cleaned.get('mother_name', '')
    app.dob = cleaned.get('dob')
    app.gender = cleaned.get('gender', '')
    app.religion = cleaned.get('religion')
    app.marital_status = cleaned.get('marital_status')
    app.community = cleaned.get('community')
    app.ex_serviceman = cleaned.get('ex_serviceman')
    app.occupation = cleaned.get('occupation')
    app.nationality = cleaned.get('nationality', 'Indian')
    app.physically_handicapped = cleaned.get('physically_handicapped', 'No')
    app.annual_income = cleaned.get('annual_income') or 0
    app.mobile_number = cleaned.get('mobile_number', '')
    app.alternative_mobile = cleaned.get('alternative_mobile', '')
    app.email = cleaned.get('email', '')
    app.communication_address = cleaned.get('communication_address', '')
    app.country = cleaned.get('country')
    app.state = cleaned.get('state')
    app.district = cleaned.get('district')
    app.pincode = cleaned.get('pincode', '')
    app.area_village_name = cleaned.get('area_village_name', '')
    app.institution = cleaned.get('institution', '')
    app.hall_ticket_number = cleaned.get('hall_ticket_number', '')
    app.registration_number = cleaned.get('registration_number', '')
    app.applying_qualification = cleaned.get('applying_qualification', '12th or equivalent')
    app.qualification = cleaned.get('qualification')
    app.custom_stream = cleaned.get('custom_stream', '')
    app.mode_of_qualification = cleaned.get('mode_of_qualification', 'Full Time')
    app.completion_status = cleaned.get('completion_status', 'Completed')
    app.percentage = cleaned.get('percentage')
    app.year_of_passing = cleaned.get('year_of_passing')
    app.training_partner = cleaned.get('training_partner')
    app.batch_code = cleaned.get('batch_code', '')
    app.program_opting = cleaned.get('program_opting')
    app.aadhaar_number = cleaned.get('aadhaar_number', '')
    app.abc_id = cleaned.get('abc_id', '')
    app.distinguishing_mark = cleaned.get('distinguishing_mark', '')
    app.consent_given = bool(cleaned.get('consent_given'))
    return app

def save_documents_to_db(app, files):
    """Persist processed temp files as UploadedDocument records (idempotent by doc_type)."""
    from django.core.files import File
    for doc_type, file_info in files.items():
        doc = UploadedDocument.objects.filter(application=app, doc_type=doc_type).first()
        if not doc:
            doc = UploadedDocument(application=app, doc_type=doc_type)
        if file_info.get('metadata'):
            meta = file_info['metadata']
            doc.original_file_size = meta.get('original_file_size')
            doc.optimized_file_size = meta.get('optimized_file_size')
            doc.image_width = meta.get('image_width')
            doc.image_height = meta.get('image_height')
            doc.mime_type = meta.get('mime_type')
            doc.compression_percentage = meta.get('compression_percentage')
            if 'jpeg_quality' in meta:
                doc.jpeg_quality = meta['jpeg_quality']
            if 'validation_status' in meta:
                doc.validation_status = meta['validation_status']
            if 'processing_policy' in meta:
                doc.processing_policy = meta['processing_policy']
        if file_info.get('original') and os.path.exists(file_info['original']):
            with open(file_info['original'], 'rb') as f:
                doc.original_file.save('original.jpg' if file_info.get('metadata') else file_info['filename'], File(f), save=False)
        if file_info.get('compressed') and os.path.exists(file_info['compressed']):
            with open(file_info['compressed'], 'rb') as f:
                doc.compressed_file.save('optimized.jpg' if file_info.get('metadata') else f"compressed_{file_info['filename']}", File(f), save=False)
        doc.save()

def build_preview_context(app, files=None):
    """Build the preview template context from a DB-backed StudentApplication."""
    display_data = {}
    display_data['country'] = str(app.country) if app.country else "-"
    display_data['state'] = str(app.state) if app.state else "-"
    display_data['district'] = str(app.district) if app.district else "-"
    display_data['religion'] = str(app.religion) if app.religion else "-"
    display_data['marital_status'] = str(app.marital_status) if app.marital_status else "-"
    display_data['community'] = str(app.community) if app.community else "-"
    display_data['occupation'] = str(app.occupation) if app.occupation else "-"
    display_data['qualification'] = str(app.qualification) if app.qualification else "-"
    if app.qualification and str(app.qualification) == 'Other' and app.custom_stream:
        display_data['qualification'] = f"Other ({app.custom_stream})"
    display_data['applying_qualification'] = dict(StudentApplication.APPLYING_QUALIFICATION_CHOICES).get(app.applying_qualification, app.applying_qualification or "-")
    display_data['program_opting'] = str(app.program_opting) if app.program_opting else "-"
    display_data['mode_of_qualification'] = dict(StudentApplication.MODE_OF_QUALIFICATION_CHOICES).get(app.mode_of_qualification, app.mode_of_qualification or "-")
    display_data['completion_status'] = dict(StudentApplication.COMPLETION_STATUS_CHOICES).get(app.completion_status, app.completion_status or "-")
    display_data['training_partner'] = str(app.training_partner) if app.training_partner else "-"
    display_data['ex_serviceman'] = str(app.ex_serviceman) if app.ex_serviceman else "-"
    display_data['area_village_name'] = app.area_village_name or "-"

    docs = UploadedDocument.objects.filter(application=app).order_by('doc_type')
    db_documents = {}
    for doc in docs:
        db_documents[doc.doc_type] = {
            'filename': doc.original_filename,
            'file_url': doc.original_file.url if doc.original_file else None,
            'has_file': True,
        }

    document_sections = []
    for doc_type, label, is_pdf in [
        ('Passport_Photo', 'Passport Size Photograph', False),
        ('Signature', 'Signature', False),
        ('Aadhaar', 'Aadhaar Card', True),
        ('Left_Thumb', 'Thumb Impression', False),
        ('Abc_id', 'Appari Id (ABC Id)', False),
        ('Father_Signature', "Father's/Guardian's Signature", False),
        ('Community_certificate', 'Community Certificate', True),
        ('Additional_Documents', 'Additional Documents', True),
    ]:
        db = db_documents.get(doc_type)
        document_sections.append({
            'doc_type': doc_type,
            'label': label,
            'is_pdf': is_pdf,
            'uploaded': bool(db and db.get('file_url')),
            'filename': db['filename'] if db else '',
            'file_url': db['file_url'] if db else '',
        })

    aadhaar = app.aadhaar_number or ''
    masked_aadhaar = f"********{aadhaar[-4:]}" if len(aadhaar) >= 4 else aadhaar

    data = {
        'full_name': app.full_name,
        'father_name': app.father_name,
        'mother_name': app.mother_name,
        'dob': app.dob,
        'gender': app.gender,
        'mobile_number': app.mobile_number,
        'alternative_mobile': app.alternative_mobile,
        'email': app.email,
        'communication_address': app.communication_address,
        'aadhaar_number': app.aadhaar_number,
        'abc_id': app.abc_id,
        'distinguishing_mark': app.distinguishing_mark,
        'pincode': app.pincode,
        'area_village_name': app.area_village_name,
        'institution': app.institution,
        'hall_ticket_number': app.hall_ticket_number,
        'registration_number': app.registration_number,
        'applying_qualification': app.applying_qualification,
        'mode_of_qualification': app.mode_of_qualification,
        'completion_status': app.completion_status,
        'percentage': app.percentage,
        'year_of_passing': app.year_of_passing,
        'batch_code': app.batch_code,
        'custom_stream': app.custom_stream,
        'consent_given': app.consent_given,
    }

    return {
        'data': data,
        'display_data': display_data,
        'db_documents': db_documents,
        'document_sections': document_sections,
        'masked_aadhaar': masked_aadhaar,
        'app': app,
        'files': files or {},
    }

def get_qualification_category(name):
    name_lower = name.lower().strip()
    
    # Exact matches that go to 'Other' (must be checked before substring matches)
    if name_lower in ['diploma', 'degree', 'other', 'ph.d', 'phd']:
        return 'Other'
    
    # 1. 12th / Equivalent
    if '12th' in name_lower or 'equivalent' in name_lower:
        return '12th / Equivalent'
        
    # 2. Diploma (prefix match — e.g. "Diploma in ..." or "2yrs of 3 yr dip...")
    if name_lower.startswith('diploma in') or '2yrs of 3 yr dip' in name_lower:
        return 'Diploma'
        
    # 3. Postgraduate
    if any(name_lower.startswith(p) for p in ['m.tech', 'm.sc', 'mba', 'mca', 'm.com', 'ma ', 'm.pharmacy', 'm.ed', 'llm']):
        return 'Postgraduate'
    # Exact matches for postgraduate
    if name_lower in ['ma', 'mba', 'mca', 'llm']:
        return 'Postgraduate'
        
    # 4. B.Tech / Engineering
    if name_lower.startswith('b.tech') or 'engineering' in name_lower:
        return 'B.Tech / Engineering'
        
    # 5. B.Sc
    if name_lower.startswith('b.sc'):
        return 'B.Sc'
        
    # 6. Other Bachelor's Degrees
    if any(name_lower.startswith(p) for p in ['b.com', 'bba', 'ba', 'bsw', 'bca', 'mbbs', 'bds', 'b.pharmacy', 'nursing', 'physiotherapy', 'llb', 'b.ed']):
        return "Other Bachelor's Degrees"
        
    return 'Other'

def registration_view(request):
    """
    Renders the public registration form.
    Pre-fills data if 'Back to Edit' was clicked from Preview.
    Loads dynamic dropdowns from Master Data.
    Also checks database for existing uploaded documents.
    """
    import json
    from registrations.models import UploadedDocument
    
    qualifications = Qualification.objects.filter(is_active=True).order_by('display_order')
    qualifications_list = []
    
    grouped_qualifications = {
        '12th / Equivalent': [],
        'Diploma': [],
        'B.Tech / Engineering': [],
        'B.Sc': [],
        "Other Bachelor's Degrees": [],
        'Postgraduate': [],
        'Other': []
    }
    
    for q in qualifications:
        category = get_qualification_category(q.name)
        qualifications_list.append({
            'id': q.id,
            'name': q.name,
            'category': category
        })
        if category in grouped_qualifications:
            grouped_qualifications[category].append(q)
        else:
            grouped_qualifications['Other'].append(q)
    
    context = {
        'countries': Country.objects.filter(is_active=True).order_by('display_order'),
        'states': State.objects.filter(is_active=True).order_by('display_order'),
        'districts': District.objects.filter(is_active=True).order_by('display_order'),
        'religions': Religion.objects.filter(is_active=True).order_by('display_order'),
        'marital_statuses': MaritalStatus.objects.filter(is_active=True).order_by('display_order'),
        'communities': Community.objects.filter(is_active=True).order_by('display_order'),
        'occupations': Occupation.objects.filter(is_active=True).order_by('display_order'),
        'ex_services': ExServiceStatus.objects.filter(is_active=True).order_by('display_order'),
        'qualifications': qualifications,
        'grouped_qualifications': grouped_qualifications,
        'qualifications_json': json.dumps(qualifications_list),
        'programs': Program.objects.filter(is_active=True).order_by('display_order'),
        'training_partners': TrainingPartner.objects.filter(is_active=True).order_by('display_order'),
        'applying_qualification_choices': StudentApplication.APPLYING_QUALIFICATION_CHOICES,
        'mode_choices': StudentApplication.MODE_OF_QUALIFICATION_CHOICES,
        'status_choices': StudentApplication.COMPLETION_STATUS_CHOICES,
    }
    
    # Check for existing application and its uploaded documents
    # This makes the database the source of truth for already-uploaded files
    app_id = request.GET.get('app_id') or request.session.get('draft_app_id')
    db_documents = {}
    if app_id:
        app = StudentApplication.objects.filter(id=app_id).first()
        if app:
            docs = UploadedDocument.objects.filter(application=app).order_by('doc_type')
            for doc in docs:
                db_documents[doc.doc_type] = {
                    'filename': doc.original_filename,
                    'file_url': doc.original_file.url if doc.original_file else None,
                    'has_file': True
                }
    
    context['db_documents'] = db_documents
    
    if request.GET.get('edit') != '1':
        if 'temp_application_data' in request.session:
            del request.session['temp_application_data']
        if 'temp_files' in request.session:
            del request.session['temp_files']
        
        temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp_uploads', request.session.session_key or 'anon')
        if os.path.exists(temp_dir):
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
        
        request.session.modified = True
    
    if 'temp_application_data' in request.session:
        temp_data = request.session['temp_application_data']
        # Fix existing invalid data in session
        import re
        modified = False
        for name_field in ['full_name', 'father_name', 'mother_name']:
            val = temp_data.get(name_field, '')
            if val and not re.match(r"^[a-zA-Z\s]+$", val):
                temp_data[name_field] = ''
                modified = True
        
        if modified:
            request.session['temp_application_data'] = temp_data
            request.session.modified = True
        
        context['form_data'] = temp_data
    
    if 'temp_files' in request.session:
        context['temp_files'] = request.session['temp_files']
    
    # DB-backed prefill when returning from Preview ("Back & Edit")
    if request.GET.get('edit') == '1':
        prefill_app = None
        if request.GET.get('app_id'):
            prefill_app = StudentApplication.objects.filter(id=request.GET['app_id']).first()
        elif request.session.get('draft_app_id'):
            prefill_app = StudentApplication.objects.filter(id=request.session['draft_app_id']).first()
        if prefill_app:
            context['form_data'] = app_to_form_data(prefill_app)

    # Form Builder: database is the single source of truth for rendering.
    context.setdefault('form_data', {})
    fb_service = FormConfigurationService()
    fb_ctx = fb_service.preview_context()
    context['fb_sections'] = fb_ctx['fb_sections']
    active_slugs = {s.slug for s in fb_service.get_sections(active_only=True)}
    context['fb_section_visible_personal'] = 'personal-details' in active_slugs
    context['fb_section_visible_contact'] = 'contact-details' in active_slugs
    context['fb_section_visible_education'] = 'education-details' in active_slugs
    context['fb_section_visible_documents'] = 'document-uploads' in active_slugs
    context['fb_section_visible_declaration'] = 'declaration' in active_slugs
    context['fb_section_visible_additional'] = 'additional-documents' in active_slugs
    context['fb_field_visible'] = {
        f.field_name: f.visible
        for entry in fb_ctx['fb_sections']
        for f in entry['fields']
    }
    
    return render(request, 'registration/index.html', context)

def submit_application(request):
    """
    Step 1: Intercepts form submission via AJAX.
    Validates data, saves it to Django session, saves files to temp storage.
    Redirects to Preview upon success via JSON response.
    """
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.accepts('application/json')
    
    if request.method == 'POST':
        print(f"SAVE PREVIEW VIEW CALLED - AJAX: {is_ajax}")
        try:
            # 1. Gather POST data
            form_data = {key: value for key, value in request.POST.items()}
            
            # Force Country to India ID in POST data before validation
            india = Country.objects.filter(name__iexact='india').first()
            if india:
                form_data['country'] = str(india.id)
            
            # 2. Handle File Uploads into Temp Storage FIRST (so they are preserved even if text validation fails)
            temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp_uploads', request.session.session_key or 'anon')
            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)
                
            fs = FileSystemStorage(location=temp_dir)
            temp_files = request.session.get('temp_files', {})
            
            file_errors = {}
            
            for filename, file_obj in request.FILES.items():
                mime_type, _ = mimetypes.guess_type(file_obj.name)
                doc_type = filename.replace('upload_', '').capitalize()
                
                # specific validation for PDFs (Aadhaar, Community Certificate, Additional Documents)
                if doc_type in ['Aadhaar', 'Community_certificate', 'Additional_documents']:
                    if mime_type != 'application/pdf':
                        file_errors[filename] = [f'{doc_type.replace("_", " ")} must be a PDF file.']
                        continue
                    if file_obj.size > 2 * 1024 * 1024:
                        file_errors[filename] = [f'{doc_type.replace("_", " ")} exceeds maximum size of 2MB.']
                        continue
                
                if doc_type == 'File':
                    if mime_type not in ['application/pdf', 'image/jpeg', 'image/jpg', 'image/png']:
                        file_errors[filename] = ['Upload File must be PDF, JPG, JPEG, or PNG.']
                        continue
                    if file_obj.size > 2 * 1024 * 1024:
                        file_errors[filename] = ['Upload File exceeds maximum size of 2MB.']
                        continue
                
                # Normalize doc type strings
                if doc_type == 'Passport_photo': doc_type = 'Passport_Photo'
                elif doc_type == 'Left_thumb': doc_type = 'Left_Thumb'
                elif doc_type == 'Father_signature': doc_type = 'Father_Signature'
                elif doc_type == 'Additional_documents': doc_type = 'Additional_Documents'
                
                # Image Optimization
                if mime_type in ['image/jpeg', 'image/jpg', 'image/png'] or doc_type in ['Passport_Photo', 'Signature', 'Left_Thumb', 'Abc_id', 'Father_Signature']:
                    
                    processed = process_and_optimize_image(file_obj, doc_type)
                    
                    saved_orig = fs.save(f"orig_{file_obj.name}", processed['original_file'])
                    saved_comp = fs.save(f"opt_{file_obj.name}", processed['optimized_file'])
                    
                    temp_files[doc_type] = {
                        'original': os.path.join(temp_dir, saved_orig),
                        'compressed': os.path.join(temp_dir, saved_comp),
                        'url': f"{settings.MEDIA_URL}temp_uploads/{request.session.session_key or 'anon'}/{saved_comp}",
                        'filename': file_obj.name,
                        'size': f"{processed['optimized_file_size'] / 1024:.2f} KB",
                        'metadata': {
                            'original_file_size': processed['original_file_size'],
                            'optimized_file_size': processed['optimized_file_size'],
                            'image_width': processed['image_width'],
                            'image_height': processed['image_height'],
                            'mime_type': processed['mime_type'],
                            'compression_percentage': float(processed['compression_percentage'])
                        }
                    }
                else:
                    # Non-images (PDFs)
                    saved_name = fs.save(file_obj.name, file_obj)
                    compressed_name = f"compressed_{saved_name}"
                    if mime_type == 'application/pdf':
                        compressed = compress_pdf(fs.open(saved_name))
                        fs.save(compressed_name, compressed)
                    else:
                        shutil.copyfile(fs.path(saved_name), fs.path(compressed_name))
                        
                    temp_files[doc_type] = {
                        'original': os.path.join(temp_dir, saved_name),
                        'compressed': os.path.join(temp_dir, compressed_name),
                        'url': f"{settings.MEDIA_URL}temp_uploads/{request.session.session_key or 'anon'}/{compressed_name}",
                        'filename': file_obj.name,
                        'size': f"{file_obj.size / 1024:.2f} KB",
                        'metadata': None
                    }
            
            # Update session with successfully processed files
            request.session['temp_files'] = temp_files
            request.session.modified = True
            
            # Enforce Mandatory Uploads AFTER processing what was sent
            mandatory_uploads = ['upload_passport_photo', 'upload_signature', 'upload_left_thumb', 'upload_father_signature', 'upload_aadhaar', 'upload_community_certificate', 'upload_additional_documents']
            uploaded_keys = request.FILES.keys()
            for mu in mandatory_uploads:
                if mu not in uploaded_keys and mu.replace('upload_', '').capitalize() not in temp_files and mu.replace('upload_', '').title().replace(' ', '_') not in temp_files:
                    if mu == 'upload_passport_photo' and 'Passport_Photo' in temp_files: continue
                    if mu == 'upload_left_thumb' and 'Left_Thumb' in temp_files: continue
                    if mu == 'upload_father_signature' and 'Father_Signature' in temp_files: continue
                    if mu == 'upload_additional_documents' and 'Additional_Documents' in temp_files: continue
                    if mu not in file_errors:
                        file_errors[mu] = [f'Missing mandatory upload: {mu.replace("upload_", "").replace("_", " ").title()}']
            
            # 3. Validate Form Text Fields
            form = StudentApplicationForm(data=form_data)
            all_errors = {}
            if not form.is_valid():
                all_errors.update(form.errors)
            if file_errors:
                all_errors.update(file_errors)

            # 3b. Dynamic DB-driven validation (Form Builder).
            # The database configuration is the source of truth for field
            # requirements and rules. Errors are merged additively so the
            # existing hardcoded form logic is never bypassed or weakened.
            try:
                fb_service = FormConfigurationService()
                config_errors = {}
                for f in fb_service.get_visible_fields():
                    if f.field_type == 'file':
                        continue
                    errs = fb_service.validate_value(f, form_data.get(f.field_name, ''))
                    if errs:
                        config_errors[f.field_name] = errs
                for fname, fobj in request.FILES.items():
                    field = fb_service.get_field(fname)
                    if field:
                        errs = fb_service.validate_file(field, fobj)
                        if errs:
                            config_errors[fname] = errs
                for fname, errs in config_errors.items():
                    if fname in all_errors:
                        for e in errs:
                            if e not in all_errors[fname]:
                                all_errors[fname].append(e)
                    else:
                        all_errors[fname] = errs
            except Exception:
                # Never let config validation break submission.
                logger.exception("Error during form-builder validation")

            if all_errors:
                if is_ajax:
                    return JsonResponse({"success": False, "errors": all_errors})
                else:
                    for field, errors in all_errors.items():
                        for error in errors:
                            messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
                    return redirect('/?edit=1')
                    
            # 4. If all validations pass: save draft application + documents to DB
            with transaction.atomic():
                app_id = request.session.get('draft_app_id')
                if app_id:
                    app = StudentApplication.objects.filter(id=app_id).first()
                else:
                    app = None
                if not app:
                    status_obj, _ = ApplicationStatus.objects.get_or_create(code='INCOMPLETE', defaults={'name': 'Incomplete'})
                    app = StudentApplication(status=status_obj)
                if not app.application_number:
                    app.application_number = f"DRAFT-{get_random_string(8)}"
                populate_app_from_data(app, form.cleaned_data)
                app.save()
                request.session['draft_app_id'] = app.id
                save_documents_to_db(app, temp_files)
            
            request.session['temp_application_data'] = form_data
            request.session.modified = True
            
            from django.urls import reverse
            preview_url = reverse('registration_preview', args=[app.id])
            if is_ajax:
                return JsonResponse({"success": True, "redirect_url": preview_url})
            return redirect(preview_url)
            
        except Exception as e:
            logger.exception("Error during submit_application")
            if is_ajax:
                return JsonResponse({"success": False, "errors": {"__all__": ["An unexpected error occurred while processing your files. Please try again."]}})
            messages.error(request, 'An unexpected error occurred while processing your files. Please try again.')
            return redirect('/?edit=1')
            
    if is_ajax:
        return JsonResponse({"success": False, "errors": {"__all__": ["Invalid request method."]}})
    messages.error(request, 'Invalid request method.')
    return redirect('registration')

def preview_application(request):
    """
    Legacy /preview/ URL. Redirects to the DB-backed preview page
    using the current draft application id.
    """
    app_id = request.session.get('draft_app_id')
    if app_id:
        return redirect('registration_preview', application_id=app_id)
    return redirect('registration')

def registration_preview(request, application_id):
    """
    Step 2: Render the Preview Page using the saved database record.
    The student's application is loaded by application_id and all data
    (personal, contact, education, identification, documents) is shown
    from the database, not from unsaved browser values.
    """
    app = get_object_or_404(StudentApplication, id=application_id)
    
    session_app_id = request.session.get('draft_app_id')
    if session_app_id:
        try:
            own_application = int(session_app_id) == application_id
        except (ValueError, TypeError):
            own_application = False
    else:
        own_application = False

    # Security: only allow viewing an application that belongs to this session.
    if not own_application:
        messages.error(request, 'Application not found.')
        return redirect('registration')
    if app.is_submitted:
        messages.error(request, 'This application has already been submitted.')
        return redirect('registration')
    
    files = request.session.get('temp_files', {})
    context = build_preview_context(app, files)
    return render(request, 'registration/preview.html', context)

def final_submit(request):
    """
    Step 3: Finalize submission of the DB-backed draft application.
    The draft (and its documents) were saved when the student clicked
    "Generate Preview". Here we assign the application number, mark the
    status as PENDING, generate acknowledgement files, clear the session
    and send the notification.
    """
    if request.method != 'POST':
        return redirect('registration')
    
    app_id = request.POST.get('application_id') or request.session.get('draft_app_id')
    if not app_id:
        messages.error(request, 'No application found to submit. Please start over.')
        return redirect('registration')
    
    app = StudentApplication.objects.filter(id=app_id).first()
    if not app:
        messages.error(request, 'Application not found. Please start over.')
        return redirect('registration')
    # Secondary Validation against bypassed requests.
    # Re-validate the latest form data (from the session if available,
    # otherwise the stored draft itself) using the full server-side rules.
    data = request.session.get('temp_application_data')
    if data:
        form = StudentApplicationForm(data=data, instance=app)
    else:
        form = StudentApplicationForm(data=app_to_form_data(app), instance=app)
    if not form.is_valid():
        messages.error(request, 'Validation failed. Please review your application.')
        return redirect('registration_preview', application_id=app.id)

    files = request.session.get('temp_files', {})
    
    try:
        with transaction.atomic():
            # Authoritative duplicate check: only SUBMITTED applications block.
            # A draft never blocks; the current application is always excluded.
            dupe_q = Q(pk__in=[])
            identity_values = {
                'mobile_number': form.cleaned_data.get('mobile_number'),
                'aadhaar_number': form.cleaned_data.get('aadhaar_number'),
                'email': form.cleaned_data.get('email'),
                'abc_id': form.cleaned_data.get('abc_id'),
                'registration_number': form.cleaned_data.get('registration_number'),
            }
            for fld, val in identity_values.items():
                if val:
                    dupe_q |= Q(**{fld: val})
            submitted_dupes = StudentApplication.objects.filter(
                dupe_q, status__code__in=SUBMITTED_STATUS_CODES
            ).exclude(pk=app.pk)
            if submitted_dupes.exists():
                messages.error(request, 'An application with the same Mobile number, Aadhaar number, or Email already exists and has been submitted.')
                return redirect('registration_preview', application_id=app.id)

            # Verify all mandatory documents exist for this application
            existing_docs = {d.lower() for d in UploadedDocument.objects.filter(application=app).values_list('doc_type', flat=True)}
            mandatory_doc_types = ['passport_photo', 'signature', 'aadhaar', 'left_thumb', 'father_signature', 'community_certificate', 'additional_documents']
            missing_docs = [d for d in mandatory_doc_types if d not in existing_docs]
            if missing_docs:
                messages.error(request, 'Missing mandatory documents. Please upload them before final submission.')
                return redirect('registration_preview', application_id=app.id)

            app_no = generate_application_number()
            aadhaar_num = app.aadhaar_number or ''
            full_nm = app.full_name or ''

            storage_folder_name = get_student_folder_name(aadhaar_num, full_nm)
            
            # Persist the re-validated data so the submitted record always
            # reflects the server-validated values.
            populate_app_from_data(app, form.cleaned_data)
            app.application_number = app_no
            app.storage_folder = storage_folder_name
            status, _ = ApplicationStatus.objects.get_or_create(code='SUBMITTED', defaults={'name': 'Submitted'})
            app.status = status
            app.ip_address = request.META.get('REMOTE_ADDR')
            app.save()
            
            # Ensure any session-only files are persisted (idempotent)
            if files and not UploadedDocument.objects.filter(application=app).exists():
                save_documents_to_db(app, files)
            
            # Clean up session and temp dir
            temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp_uploads', request.session.session_key or 'anon')
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
                
            if 'temp_application_data' in request.session:
                del request.session['temp_application_data']
            if 'temp_files' in request.session:
                del request.session['temp_files']
            if 'draft_app_id' in request.session:
                del request.session['draft_app_id']
                
            # Generate Acknowledgement files
            generate_acknowledgement_files(app)
            
            # Send Email Notification
            from django.core.mail import send_mail
            admin_email = getattr(settings, 'ADMIN_EMAIL', 'admin@example.com')
            try:
                admin_url = request.build_absolute_uri(f"/dashboard/applications/")
                msg = f"A new application ({app.application_number}) has been submitted by {app.full_name}.\\n\\n"
                msg += f"Mobile: {app.mobile_number}\\nEmail: {app.email}\\nStatus: Submitted\\n\\n"
                msg += f"View details here: {admin_url}"
                send_mail(
                    f"New Application Submitted: {app.application_number}",
                    msg,
                    settings.DEFAULT_FROM_EMAIL,
                    [admin_email],
                    fail_silently=True,
                )
            except Exception as e:
                logger.error(f"Failed to send admin email: {e}")
            
            request.session['success_data'] = {
                'app_id': app.id,
                'app_no': app_no,
                'name': app.full_name,
                'date': app.submission_date.strftime('%Y-%m-%d %H:%M:%S') if app.submission_date else '',
                'pdf_url': app.acknowledgement_pdf.url if app.acknowledgement_pdf else '',
                'png_url': app.acknowledgement_png.url if app.acknowledgement_png else '',
                'jpg_url': app.acknowledgement_jpg.url if app.acknowledgement_jpg else '',
                'auth_token': get_random_string(32),
            }
            
            return redirect('success')
            
    except Exception as e:
        logger.exception("Error during final_submit")
        messages.error(request, 'An unexpected error occurred while saving your application. Please contact support.')
        return redirect('registration_preview', application_id=app.id)

def success_view(request):
    """
    Step 4: Render Success Page.
    """
    if 'success_data' not in request.session:
        return redirect('registration')
        
    data = request.session['success_data']
    
    app = get_object_or_404(StudentApplication, id=data['app_id'])
    documents = UploadedDocument.objects.filter(application=app).order_by('id')
    
    return render(request, 'registration/success.html', {
        'data': data,
        'documents': documents,
        'auth_token': data['auth_token'],
    })

def download_document(request, token, doc_id):
    success_data = request.session.get('success_data')
    if not success_data or success_data.get('auth_token') != token:
        raise Http404("Unauthorized or expired session.")
        
    doc = get_object_or_404(UploadedDocument, id=doc_id, application_id=success_data['app_id'])
    
    is_view = request.GET.get('view') == '1'
    
    file_to_send = doc.compressed_file if doc.compressed_file else doc.original_file
    
    if is_view and doc.processing_policy == 'document_preserve' and doc.original_file:
        file_to_send = doc.original_file
    
    if not file_to_send or not os.path.exists(file_to_send.path):
        raise Http404("File not found.")
        
    response = FileResponse(file_to_send.open('rb'))
    if is_view:
        filename = doc.original_filename if file_to_send == doc.original_file else doc.compressed_filename
        response['Content-Disposition'] = f'inline; filename="{filename}"'
    else:
        filename = doc.compressed_filename or doc.original_filename
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        
    response['Cache-Control'] = 'no-store, private'
    return response

def download_all_documents(request, token):
    success_data = request.session.get('success_data')
    if not success_data or success_data.get('auth_token') != token:
        raise Http404("Unauthorized or expired session.")
        
    app = get_object_or_404(StudentApplication, id=success_data['app_id'])
    documents = UploadedDocument.objects.filter(application=app)
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for doc in documents:
            file_to_send = doc.compressed_file if doc.compressed_file else doc.original_file
            if file_to_send and os.path.exists(file_to_send.path):
                # We save with original doc_type as name to avoid collisions
                ext = os.path.splitext(file_to_send.name)[1]
                zip_file.write(file_to_send.path, f"{doc.doc_type}{ext}")
                
    zip_buffer.seek(0)
    response = FileResponse(zip_buffer, content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="Processed_Documents_{app.application_number}.zip"'
    return response

def check_duplicate(request):
    field = request.GET.get('field')
    value = request.GET.get('value')
    allowed_fields = ['aadhaar_number', 'mobile_number', 'email', 'abc_id', 'registration_number', 'hall_ticket_number']
    if field in allowed_fields and value:
        exists = StudentApplication.objects.filter(
            **{field: value}, status__code__in=SUBMITTED_STATUS_CODES
        ).exists()
        return JsonResponse({'exists': exists, 'field': field})
    return JsonResponse({'error': 'Invalid parameters'}, status=400)

def get_states(request):
    country_id = request.GET.get('country_id')
    states = list(State.objects.filter(country_id=country_id, is_active=True).order_by('display_order').values('id', 'name'))
    return JsonResponse(states, safe=False)

def get_districts(request):
    state_id = request.GET.get('state_id')
    districts = list(District.objects.filter(state_id=state_id, is_active=True).order_by('display_order').values('id', 'name'))
    return JsonResponse(districts, safe=False)

def auto_save_field(request):
    if request.method == 'POST' and request.headers.get('x-requested-with') == 'XMLHttpRequest':
        field = request.POST.get('field')
        value = request.POST.get('value')
        
        if field:
            import re
            if field in ['full_name', 'father_name', 'mother_name']:
                if value and not re.match(r"^[a-zA-Z\s]+$", value):
                    return JsonResponse({'success': False, 'message': 'Only alphabets and spaces are allowed.'})
                if value:
                    value = value.strip()
            elif field == 'distinguishing_mark':
                if value:
                    value = value.strip()
                    if len(value) < 5 or len(value) > 100:
                        return JsonResponse({'success': False, 'message': 'Must be between 5 and 100 characters.'})
                    if not re.match(r"^[a-zA-Z\s]+$", value):
                        return JsonResponse({'success': False, 'message': 'Only alphabetic characters and spaces are allowed.'})
            elif field == 'mobile_number':
                if not value or not re.match(r"^[1-9]\d{9}$", value):
                    return JsonResponse({'success': False, 'message': 'Number must be 10 digits and cannot start with 0.'})
            elif field == 'alternative_mobile':
                if value and not re.match(r"^[1-9]\d{9}$", value):
                    return JsonResponse({'success': False, 'message': 'Number must be 10 digits and cannot start with 0.'})
            elif field == 'aadhaar_number':
                if not value or not re.match(r"^[1-9]\d{11}$", value):
                    return JsonResponse({'success': False, 'message': 'Aadhaar number must be 12 digits and cannot start with 0.'})
            elif field == 'pincode':
                if not value or not re.match(r"^\d{6}$", value):
                    return JsonResponse({'success': False, 'message': 'Enter a valid 6-digit pincode.'})
            elif field == 'email':
                if value and not re.match(r"[^@]+@[^@]+\.[^@]+", value):
                    return JsonResponse({'success': False, 'message': 'Enter a valid email address.'})
            elif field == 'abc_id':
                if not value or not re.match(r"^\d{12}$", value):
                    return JsonResponse({'success': False, 'message': 'Enter a valid 12-digit ABC ID (Apaar ID).'})
            elif field == 'percentage':
                if value:
                    try:
                        val = float(value)
                        if val < 0 or val > 100:
                            return JsonResponse({'success': False, 'message': 'Enter a valid percentage between 0 and 100.'})
                    except ValueError:
                        return JsonResponse({'success': False, 'message': 'Enter a valid percentage between 0 and 100.'})
            elif field == 'custom_stream':
                if value:
                    if len(value) < 3 or len(value) > 100:
                        return JsonResponse({'success': False, 'message': 'Must be between 3 and 100 characters.'})
                    if not re.match(r"^[A-Za-z0-9\s\-\/&\(\)]+$", value):
                        return JsonResponse({'success': False, 'message': 'Contains invalid characters.'})
            
            temp_data = request.session.get('temp_application_data', {})
            temp_data[field] = value
            
            # Cross-field validation check before saving
            if field in ['completion_status', 'year_of_passing', 'percentage']:
                status = temp_data.get('completion_status')
                yop = temp_data.get('year_of_passing')
                pct = temp_data.get('percentage')
                
                import datetime
                current_year = datetime.datetime.now().year
                
                if status == 'Completed':
                    if yop and int(yop) > current_year:
                        if field == 'year_of_passing': return JsonResponse({'success': False, 'message': 'Year cannot be greater than current year.'})
                    if not pct and field == 'percentage':
                        return JsonResponse({'success': False, 'message': 'Percentage is mandatory for Completed status.'})
                elif status == 'Ongoing':
                    if yop and int(yop) < current_year:
                        if field == 'year_of_passing': return JsonResponse({'success': False, 'message': 'Year cannot be less than current year.'})
            
            request.session['temp_application_data'] = temp_data
            request.session.modified = True
            
            # --- DB SAVE AS SOURCE OF TRUTH ---
            app_id = request.session.get('draft_app_id')
            if app_id:
                app = StudentApplication.objects.filter(id=app_id).first()
            else:
                app = None
                
            if not app:
                status_obj, _ = ApplicationStatus.objects.get_or_create(code='INCOMPLETE', defaults={'name': 'Incomplete'})
                app = StudentApplication(status=status_obj)
                app.application_number = f"DRAFT-{get_random_string(8)}"
                app.save()
                request.session['draft_app_id'] = app.id
                request.session.modified = True
                
            try:
                db_val = value if value != "" else None
                if field == 'country' and db_val: db_val = Country.objects.filter(id=db_val).first()
                elif field == 'state' and db_val: db_val = State.objects.filter(id=db_val).first()
                elif field == 'district' and db_val: db_val = District.objects.filter(id=db_val).first()
                elif field == 'religion' and db_val: db_val = Religion.objects.filter(id=db_val).first()
                elif field == 'marital_status' and db_val: db_val = MaritalStatus.objects.filter(id=db_val).first()
                elif field == 'community' and db_val: db_val = Community.objects.filter(id=db_val).first()
                elif field == 'occupation' and db_val: db_val = Occupation.objects.filter(id=db_val).first()
                elif field == 'qualification' and db_val: db_val = Qualification.objects.filter(id=db_val).first()
                elif field == 'program_opting' and db_val: db_val = Program.objects.filter(id=db_val).first()
                elif field == 'training_partner' and db_val: db_val = TrainingPartner.objects.filter(id=db_val).first()
                elif field == 'ex_serviceman' and db_val: db_val = ExServiceStatus.objects.filter(id=db_val).first()
                
                if hasattr(app, field):
                    setattr(app, field, db_val)
                    app.save(update_fields=[field])
            except Exception as e:
                logger.error(f"DB Draft save error for {field}: {e}")
                
            return JsonResponse({'success': True, 'message': '\u2713 Already saved'})
    return JsonResponse({'success': False})

def get_batch_code(request):
    training_partner_id = request.GET.get('training_partner_id')
    if training_partner_id:
        from masterdata.models import BatchCode
        batch = BatchCode.objects.filter(training_partner_id=training_partner_id).first()
        if batch:
            return JsonResponse({'success': True, 'batch_code': batch.code})
    return JsonResponse({'success': False, 'message': 'No batch assigned. Please contact the administrator.'})

import urllib.request
import json
import re
from django.core.cache import cache

def pincode_lookup(request):
    pincode = request.GET.get('pincode', '').strip()
    if not re.match(r"^\d{6}$", pincode):
        return JsonResponse({'success': False, 'message': 'Invalid or unavailable pincode.'})

    cache_key = f"pincode_{pincode}"
    cached_data = cache.get(cache_key)

    if cached_data is not None:
        post_offices = cached_data
    else:
        try:
            url = f"https://api.postalpincode.in/pincode/{pincode}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=8) as response:
                data = json.loads(response.read().decode())

                if not data or data[0].get('Status') != 'Success' or not data[0].get('PostOffice'):
                    return JsonResponse({'success': False, 'message': 'Invalid or unavailable pincode.'})

                post_offices = data[0].get('PostOffice', [])
                if not post_offices:
                    return JsonResponse({'success': False, 'message': 'Invalid or unavailable pincode.'})

                cache.set(cache_key, post_offices, 86400)
        except Exception:
            return JsonResponse({'success': False, 'message': 'Invalid or unavailable pincode.'})

    primary_office = post_offices[0]
    state_name = (primary_office.get('State') or '').strip()
    district_name = (primary_office.get('District') or '').strip()
    if not state_name or not district_name:
        return JsonResponse({'success': False, 'message': 'Invalid or unavailable pincode.'})

    country, _ = Country.objects.get_or_create(
        name__iexact='India',
        defaults={'name': 'India', 'code': 'IN', 'iso_code': 'IND'}
    )

    state_qs = State.objects.filter(name__iexact=state_name)
    if state_qs.exists():
        state = state_qs.first()
    else:
        state = State.objects.create(name=state_name, code=state_name.upper()[:10], country=country)

    district_qs = District.objects.filter(name__iexact=district_name, state=state)
    if district_qs.exists():
        district = district_qs.first()
    else:
        district = District.objects.create(name=district_name, code=district_name.upper()[:10], state=state)

    area_options = []
    seen_areas = set()
    for office in post_offices:
        area_name = (office.get('Name') or '').strip()
        if not area_name:
            continue
        key = area_name.lower()
        if key in seen_areas:
            continue
        seen_areas.add(key)
        area_options.append({
            'value': area_name,
            'label': area_name,
            'state_id': state.id,
            'district_id': district.id,
        })

    response = {
        'success': True,
        'state_id': state.id,
        'district_id': district.id,
        'multiple_areas': len(area_options) > 1,
        'area_options': area_options,
        'area_village_name': area_options[0]['value'] if area_options else '',
        'message': 'Pincode details updated.' if len(area_options) <= 1 else 'Multiple areas found. Please choose the correct one.'
    }

    temp_data = request.session.get('temp_application_data', {})
    temp_data['pincode'] = pincode
    temp_data['state'] = str(state.id)
    temp_data['district'] = str(district.id)
    if area_options:
        temp_data['area_village_name'] = area_options[0]['value']
    request.session['temp_application_data'] = temp_data
    request.session.modified = True

    return JsonResponse(response)

def async_upload_pdf(request):
    if request.method == 'POST' and request.FILES:
        file_key = list(request.FILES.keys())[0]
        file_obj = request.FILES[file_key]
        doc_type = file_key.replace('upload_', '').capitalize()
        if doc_type == 'Additional_documents': doc_type = 'Additional_Documents'

        mime_type, _ = mimetypes.guess_type(file_obj.name)
        if mime_type != 'application/pdf':
            return JsonResponse({'success': False, 'error': 'Must be a PDF file.'})

        temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp_uploads', request.session.session_key or 'anon')
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
            
        fs = FileSystemStorage(location=temp_dir)
        saved_name = fs.save(file_obj.name, file_obj)
        
        if file_obj.size > 2 * 1024 * 1024:
            compressed = compress_pdf(fs.open(saved_name))
            compressed_name = f"compressed_{saved_name}"
            fs.save(compressed_name, compressed)
            
            final_size = fs.size(compressed_name)
            if final_size > 2 * 1024 * 1024:
                fs.delete(saved_name)
                fs.delete(compressed_name)
                return JsonResponse({'success': False, 'error': 'Unable to compress this PDF below 2 MB. Please upload a smaller PDF.'})
            
            fs.delete(saved_name)
            final_url = f"{settings.MEDIA_URL}temp_uploads/{request.session.session_key or 'anon'}/{compressed_name}"
            final_path_original = os.path.join(temp_dir, compressed_name)
            final_path_compressed = os.path.join(temp_dir, compressed_name)
            final_size_bytes = final_size
        else:
            compressed_name = f"compressed_{saved_name}"
            shutil.copyfile(fs.path(saved_name), fs.path(compressed_name))
            final_url = f"{settings.MEDIA_URL}temp_uploads/{request.session.session_key or 'anon'}/{compressed_name}"
            final_path_original = os.path.join(temp_dir, saved_name)
            final_path_compressed = os.path.join(temp_dir, compressed_name)
            final_size_bytes = file_obj.size
            
        temp_files = request.session.get('temp_files', {})
        temp_files[doc_type] = {
            'original': final_path_original,
            'compressed': final_path_compressed,
            'url': final_url,
            'filename': file_obj.name,
            'size': f"{final_size_bytes / 1024 / 1024:.2f} MB" if final_size_bytes > 1024*1024 else f"{final_size_bytes / 1024:.2f} KB",
            'metadata': None
        }
        request.session['temp_files'] = temp_files
        request.session.modified = True
        
        return JsonResponse({
            'success': True,
            'filename': file_obj.name,
            'size': temp_files[doc_type]['size'],
            'url': final_url
        })
        
    return JsonResponse({'success': False, 'error': 'Invalid request'})
