import json
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from masterdata.models import Country, State, District


class PincodeLookupTests(TestCase):
    def setUp(self):
        self.country = Country.objects.create(name='India', code='IN', iso_code='IND')
        self.state = State.objects.create(name='Andhra Pradesh', code='AP', country=self.country)
        self.district = District.objects.create(name='Guntur', code='GUN', state=self.state)

    @patch('urllib.request.urlopen')
    def test_pincode_lookup_uses_post_office_name_for_area_village(self, mock_urlopen):
        payload = [{
            'Status': 'Success',
            'PostOffice': [{
                'Name': 'Abburu',
                'District': 'Guntur',
                'State': 'Andhra Pradesh',
            }]
        }]
        mock_urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode('utf-8')

        response = self.client.get(reverse('pincode_lookup'), {'pincode': '522402'})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['area_village_name'], 'Abburu')
        self.assertEqual(data['state_id'], self.state.id)
        self.assertEqual(data['district_id'], self.district.id)

    @patch('urllib.request.urlopen')
    def test_pincode_lookup_returns_multiple_area_choices_when_needed(self, mock_urlopen):
        payload = [{
            'Status': 'Success',
            'PostOffice': [
                {'Name': 'Abburu', 'District': 'Guntur', 'State': 'Andhra Pradesh'},
                {'Name': 'Bayyavaram', 'District': 'Guntur', 'State': 'Andhra Pradesh'}
            ]
        }]
        mock_urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode('utf-8')

        response = self.client.get(reverse('pincode_lookup'), {'pincode': '522402'})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['multiple_areas'])
        self.assertIn('Abburu', [item['value'] for item in data['area_options']])
        self.assertIn('Bayyavaram', [item['value'] for item in data['area_options']])


class PercentageFieldValidationTests(TestCase):
    def test_valid_percentage_values(self):
        from registrations.forms import StudentApplicationForm
        from masterdata.models import Country
        
        # Test valid percentage values on the form field directly
        for val in ['0', '50', '75.50', '89.25', '100', '100.00']:
            form = StudentApplicationForm(data={'percentage': val})
            # percentage is not mandatory unless status is Completed, which requires other fields,
            # so we just check errors specifically for percentage.
            self.assertNotIn('percentage', form.errors)

    def test_invalid_percentage_values(self):
        from registrations.forms import StudentApplicationForm
        
        for val in ['100.01', '-0.01', 'abc', '75.5.0']:
            form = StudentApplicationForm(data={'percentage': val})
            self.assertIn('percentage', form.errors)
            self.assertEqual(form.errors['percentage'][0], 'Enter a valid percentage between 0 and 100.')
