# -*- coding: utf-8 -*-
import base64
import json
import odoo # Import odoo to patch odoo.registry
import threading
from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError

# Import the prompt constant from the model file
from odoo.addons.hr_recruitment_gemini.models.hr_applicant import GEMINI_CV_EXTRACTION_PROMPT_FILE

# Sample successful response from Gemini
# This simulates the JSON data we expect the API to return.
MOCK_GEMINI_RESPONSE_JSON = {
    "name": "John Doe",
    "email": "john.doe@example.com",
    "phone": "123-456-7890",
    "linkedin": "https://linkedin.com/in/johndoe",
    "degree": "Bachelor's Degree in Computer Science",
    "skills": [
        {"type": "Programming Languages", "skill": "Python", "level": "Advanced (80%)"},
        {"type": "Languages", "skill": "English", "level": "C1 (85%)"}
    ]
}

# Sample error response from Gemini
MOCK_GEMINI_RESPONSE_ERROR = "An error occurred."

# Sample response with invalid JSON
MOCK_GEMINI_RESPONSE_INVALID_JSON = "Here is the data: { 'name': 'test' "


class TestHrApplicantGemini(TransactionCase):
    """
    Test suite for the `hr.applicant` Gemini extraction functionality.
    This class mocks the external API call to test the internal logic.
    """

    @classmethod
    def setUpClass(cls):
        """
        Set up the test environment.
        - Create a test applicant.
        - Create a mock CV attachment.
        - Create required related data (e.g., skill module setup).
        """
        super().setUpClass()
        
        # Mock the check for hr_recruitment_skills being installed
        # This avoids a complex module installation during tests.
        mock_module = MagicMock()
        mock_module.state = 'installed'
        
        cls.mock_get_patcher = patch.object(
            type(cls.env['ir.module.module']), 
            '_get', 
            MagicMock(return_value=mock_module)
        )
        cls.mock_get_patcher.start()

        cls.applicant = cls.env['hr.applicant'].create({
            'name': "Test Applicant's Application",
        })

        cls.attachment = cls.env['ir.attachment'].create({
            'name': 'test_cv.pdf',
            'datas': base64.b64encode(b'This is a fake PDF content'),
            'mimetype': 'application/pdf',
            'res_model': 'hr.applicant',
            'res_id': cls.applicant.id,
        })
        
        cls.applicant.message_main_attachment_id = cls.attachment.id

        cls.env.company.write({
            'gemini_cv_extract_mode': 'manual_send',
            'gemini_api_key': 'fake_api_key',
            'gemini_model': 'fake-model-name',
        })
        
        cls.env['hr.skill.level'].create({
            'name': 'Beginner',
            'level_progress': 15,
        })

    @classmethod
    def tearDownClass(cls):
        """Stop class-level patchers."""
        super().tearDownClass()
        cls.mock_get_patcher.stop()

    def setUp(self):
        """
        Override setUp to prevent `self.env.cr.commit()` from being called
        inside the threaded function, which would break the test transaction.
        We also mock the threading itself to run synchronously.
        """
        super().setUp()

        # 1. Patch `self.env.cr.commit()` and `self.env.cr.rollback()` to do nothing.
        # This is CRITICAL for tests, as committing or rolling back 
        # would break the transaction rollback.
        self.commit_patcher = patch('odoo.sql_db.Cursor.commit', lambda *args, **kwargs: None)
        self.commit_patcher.start()
        self.rollback_patcher = patch('odoo.sql_db.Cursor.rollback', lambda *args, **kwargs: None)
        self.rollback_patcher.start()


        # 2. Patch `threading.Thread`
        # We patch the 'threading.Thread' class itself.
        # When `threading.Thread(target=..., args=...)` is called,
        # our side_effect function `mock_thread_constructor` will run.
        self.thread_patcher = patch('threading.Thread', autospec=True)
        mock_thread_class = self.thread_patcher.start()

        def mock_thread_constructor(*args, **kwargs):
            """
            This mock constructor captures the `target` and `args`
            and returns a mock instance whose `start` method
            will run the target function immediately.
            """
            target_func = kwargs.get('target')
            target_args = kwargs.get('args', ())
            
            mock_instance = MagicMock()
            
            # When `start()` is called on this instance, run the target function
            mock_instance.start = MagicMock(side_effect=lambda: target_func(*target_args) if target_func else None)
            
            return mock_instance

        # Set the side_effect of the mocked class to our constructor function
        mock_thread_class.side_effect = mock_thread_constructor

        # 3. Patch `odoo.registry`
        # This intercepts the `odoo.registry(dbname)` call.
        mock_registry_obj = MagicMock()
        
        # This mock context manager will be returned by `mock_registry_obj.cursor()`
        # It ensures that `with ... as new_cr:` yields the main test cursor.
        mock_cursor_context_manager = MagicMock()
        mock_cursor_context_manager.__enter__.return_value = self.env.cr
        mock_registry_obj.cursor.return_value = mock_cursor_context_manager
        
        # `odoo.registry` is a function, so we patch it to return our mock Registry object.
        self.cursor_patcher = patch('odoo.registry', return_value=mock_registry_obj)
        self.cursor_patcher.start()


    def tearDown(self):
        """Stop the patchers after each test."""
        self.cursor_patcher.stop() # Stop cursor patch
        self.thread_patcher.stop()
        self.commit_patcher.stop()
        self.rollback_patcher.stop() # Stop rollback patch
        super().tearDown()

    def test_01_successful_extraction(self):
        """
        Test a full, successful extraction and data writing.
        """
        mock_api_response = MagicMock()
        mock_api_response.text = json.dumps(MOCK_GEMINI_RESPONSE_JSON)

        # Create REAL records for the test to find.
        # This avoids all ForeignKeyViolations and unaccent errors.
        real_degree = self.env['hr.recruitment.degree'].create({
            'name': "Bachelor's Degree in Computer Science"
        })
        skill_type_prog = self.env['hr.skill.type'].create({'name': 'Programming Languages'})
        skill_type_lang = self.env['hr.skill.type'].create({'name': 'Languages'})
        skill_level_adv = self.env['hr.skill.level'].create({'name': 'Advanced', 'level_progress': 80})
        skill_level_c1 = self.env['hr.skill.level'].create({'name': 'C1', 'level_progress': 85})
        real_skill_py = self.env['hr.skill'].create({'name': 'Python', 'skill_type_id': skill_type_prog.id})
        real_skill_en = self.env['hr.skill'].create({'name': 'English', 'skill_type_id': skill_type_lang.id})

        # We patch the `search` method of each model to return the
        # correct recordset based on the search domain.
        degree_model = self.env['hr.recruitment.degree']
        orig_degree_search = degree_model.search
        def mock_degree_search(domain, *args, **kwargs):
            if domain == [('name', '=ilike', "Bachelor's Degree in Computer Science")]:
                return real_degree
            return orig_degree_search(domain, *args, **kwargs)

        skill_type_model = self.env['hr.skill.type']
        orig_skill_type_search = skill_type_model.search
        def mock_skill_type_search(domain, *args, **kwargs):
            if domain == [('name', '=ilike', 'Programming Languages')]:
                return skill_type_prog
            if domain == [('name', '=ilike', 'Languages')]:
                return skill_type_lang
            return orig_skill_type_search(domain, *args, **kwargs)

        skill_level_model = self.env['hr.skill.level']
        orig_skill_level_search = skill_level_model.search
        def mock_skill_level_search(domain, *args, **kwargs):
            name_domain = domain[0]
            if name_domain == ('name', '=ilike', 'Advanced'):
                return skill_level_adv
            if name_domain == ('name', '=ilike', 'C1'):
                return skill_level_c1
            return orig_skill_level_search(domain, *args, **kwargs)

        skill_model = self.env['hr.skill']
        orig_skill_search = skill_model.search
        def mock_skill_search(domain, *args, **kwargs):
            if domain == [('name', '=ilike', 'Python')]:
                return real_skill_py
            if domain == [('name', '=ilike', 'English')]:
                return real_skill_en
            return orig_skill_search(domain, *args, **kwargs)

        # Mock applicant skill search to always return empty, forcing creation
        applicant_skill_model = self.env['hr.applicant.skill']
        mock_app_skill_search = MagicMock(return_value=applicant_skill_model.browse([]))

        # Patch the `generate_content` method and all relevant search methods
        with patch('odoo.addons.hr_recruitment_gemini.models.hr_applicant.genai.GenerativeModel.generate_content', return_value=mock_api_response) as mock_gen_content, \
             patch.object(type(degree_model), 'search', side_effect=mock_degree_search) as mock_degree_search_patch, \
             patch.object(type(skill_type_model), 'search', side_effect=mock_skill_type_search), \
             patch.object(type(skill_level_model), 'search', side_effect=mock_skill_level_search), \
             patch.object(type(skill_model), 'search', side_effect=mock_skill_search), \
             patch.object(type(applicant_skill_model), 'search', mock_app_skill_search):
            
            self.applicant.action_extract_with_gemini()
            
            # 1. Check if the API was called correctly
            mock_gen_content.assert_called_once()
            call_args = mock_gen_content.call_args[0][0]
            self.assertIn(GEMINI_CV_EXTRACTION_PROMPT_FILE, call_args) 
            self.assertEqual(call_args[1]['mime_type'], 'application/pdf')
            
            # 2. Check applicant state
            self.assertEqual(self.applicant.gemini_extract_state, 'done')
            self.assertEqual(self.applicant.gemini_extract_status, 'Successfully extracted data.')

            # 3. Check simple fields
            self.assertEqual(self.applicant.partner_name, 'John Doe')
            self.assertEqual(self.applicant.name, "John Doe's Application")
            self.assertEqual(self.applicant.email_from, 'john.doe@example.com')
            self.assertEqual(self.applicant.partner_phone, '123-456-7890')
            self.assertEqual(self.applicant.linkedin_profile, 'https://linkedin.com/in/johndoe')

            # 4. Check created degree
            mock_degree_search_patch.assert_called_with(
                [('name', '=ilike', "Bachelor's Degree in Computer Science")], limit=1
            )
            self.assertEqual(self.applicant.type_id.id, real_degree.id)
            
            # 5. Check created skills
            applicant_skills = self.applicant.applicant_skill_ids
            self.assertEqual(len(applicant_skills), 2)
            
            python_skill = applicant_skills.filtered(lambda s: s.skill_id.name == 'Python')
            self.assertTrue(python_skill)
            self.assertEqual(python_skill.skill_type_id.name, 'Programming Languages')
            self.assertEqual(python_skill.skill_level_id.name, 'Advanced')
            self.assertEqual(python_skill.skill_level_id.level_progress, 80)
            
            english_skill = applicant_skills.filtered(lambda s: s.skill_id.name == 'English')
            self.assertTrue(english_skill)
            self.assertEqual(english_skill.skill_type_id.name, 'Languages')
            self.assertEqual(english_skill.skill_level_id.name, 'C1')
            self.assertEqual(english_skill.skill_level_id.level_progress, 85)

    def test_02_api_call_failure(self):
        """
        Test how the system handles a direct exception from the API call.
        """
        # Patch `generate_content` to raise an exception
        with patch('odoo.addons.hr_recruitment_gemini.models.hr_applicant.genai.GenerativeModel.generate_content', side_effect=Exception(MOCK_GEMINI_RESPONSE_ERROR)):
            
            self.applicant.action_extract_with_gemini()
            
            self.assertEqual(self.applicant.gemini_extract_state, 'error')
            self.assertIn(MOCK_GEMINI_RESPONSE_ERROR, self.applicant.gemini_extract_status)
            self.assertEqual(self.applicant.partner_name, False)

    def test_03_invalid_json_response(self):
        """
        Test how the system handles a response that is not valid JSON.
        """
        mock_api_response = MagicMock()
        mock_api_response.text = MOCK_GEMINI_RESPONSE_INVALID_JSON
        
        with patch('odoo.addons.hr_recruitment_gemini.models.hr_applicant.genai.GenerativeModel.generate_content', return_value=mock_api_response):

            self.applicant.action_extract_with_gemini()
            
            self.assertEqual(self.applicant.gemini_extract_state, 'error')
            self.assertIn("invalid response that could not be parsed", self.applicant.gemini_extract_status)
            self.assertIn(MOCK_GEMINI_RESPONSE_INVALID_JSON, self.applicant.gemini_extract_status)
            self.assertEqual(self.applicant.partner_name, False)

    def test_04_no_api_key(self):
        """
        Test that the extraction fails if the API key is not set.
        """
        self.env.company.gemini_api_key = False
        
        self.applicant.action_extract_with_gemini()
        
        self.assertEqual(self.applicant.gemini_extract_state, 'error')
        self.assertIn("Gemini API Key is not set", self.applicant.gemini_extract_status)

    def test_05_can_extract_with_gemini_compute(self):
        """
        Test the logic of the `can_extract_with_gemini` compute field.
        """
        # 1. Correct state: manual mode, attachment, valid state
        self.env.company.gemini_cv_extract_mode = 'manual_send'
        self.applicant.message_main_attachment_id = self.attachment
        self.applicant.gemini_extract_state = 'no_extract'
        self.assertTrue(self.applicant.can_extract_with_gemini)
        
        # 2. Test 'done' state (should allow retry)
        self.applicant.gemini_extract_state = 'done'
        self.assertTrue(self.applicant.can_extract_with_gemini)
        
        # 3. Test 'error' state (should allow retry)
        self.applicant.gemini_extract_state = 'error'
        self.assertTrue(self.applicant.can_extract_with_gemini)

        # 4. Wrong mode (no_send)
        self.env.company.gemini_cv_extract_mode = 'no_send'
        self.assertFalse(self.applicant.can_extract_with_gemini)
        
        # 5. Wrong state (processing)
        self.env.company.gemini_cv_extract_mode = 'manual_send'
        self.applicant.gemini_extract_state = 'processing'
        self.assertFalse(self.applicant.can_extract_with_gemini)
        
        # 6. No attachment
        self.applicant.gemini_extract_state = 'no_extract'
        self.applicant.message_main_attachment_id = False
        self.assertFalse(self.applicant.can_extract_with_gemini)

