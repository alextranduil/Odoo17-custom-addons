# -*- coding: utf-8 -*-
import base64
import google.generativeai as genai
import json
import logging
import odoo
import re
import threading

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# This prompt instructs the Gemini model to act as an HR assistant
# and extract specific fields from a CV file, returning them in a
# structured JSON format.
GEMINI_CV_EXTRACTION_PROMPT_FILE = """
You are an expert HR assistant. Your task is to extract key information from the
attached curriculum vitae (CV) file and return it as a valid JSON object.

Extract the following fields from the file:
- "name": The full name of the applicant.
- "email": The primary email address.
- "phone": The primary phone number.
- "linkedin": The applicant's LinkedIn profile URL.
- "degree": The applicant's highest or most relevant academic degree (e.g., "Bachelor's Degree in Cybersecurity").
- "skills": A list of professional skills. Each skill must be an object with three keys:
  - "type": The category of the skill (e.g., "Programming Languages", "Languages", "IT", "Soft Skills", "Marketing").
  - "skill": The name of the skill (e.g., "Python", "English", "Docker", "Teamwork").
  - "level": The proficiency level (e.g., "Beginner (15%)", "Elementary (25%)", "Intermediate (50%)", "Advanced (80%)", "Expert (100%)").

RULES:
1.  Return ONLY a valid JSON object.
2.  Do not include any text before or after the JSON (e.g., no "Here is the JSON..." or markdown backticks).
3.  If a value is not found, return `null` for that field, except for the "skills" field. For the "skills" field, return the "Beginner (15%)" level.
4.  The "skills" field must be a list of objects, like:
    "skills": [
      {{ "type": "Programming Languages", "skill": "Python", "level": "Advanced (80%)" }},
      {{ "type": "Languages", "skill": "English", "level": "C1 (85%)" }}
    ]
5. Skill levels for different type:
  - "Programming Languages": "Beginner (15%)", "Elementary (25%)", "Intermediate (50%)", "Advanced (80%)", "Expert (100%)";
  - "Languages": "C2 (100%)", "C1 (85%)", "B2 (75%)", "B1 (60%)", "A2 (40%)", "A1 (10%)";
  - "IT": "Beginner (15%)", "Elementary (25%)", "Intermediate (50%)", "Advanced (80%)", "Expert (100%)";
  - "Soft Skills": "Beginner (15%)", "Elementary (25%)", "Intermediate (50%)", "Advanced (80%)", "Expert (100%)";
  - "Marketing": (L4 (100%), L3 (75%), L2 (50%), L1 (25%)).


JSON:
"""


class HrApplicant(models.Model):
    """
    Inherits `hr.applicant` to add functionality for extracting CV data
    using the Google Gemini API. Manages the extraction state, button
    visibility, and processes the data received from the API.
    """
    _inherit = 'hr.applicant'
    # Sort by state to show processing/pending records first
    _order = "gemini_extract_state desc, priority desc, id desc"

    gemini_extract_state = fields.Selection(
        selection=[
            ('no_extract', 'No Extraction'),
            ('pending', 'Pending'),
            ('processing', 'Processing'),
            ('done', 'Done'),
            ('error', 'Error'),
        ],
        string='Gemini Extract State',
        default='no_extract',
        required=True,
        copy=False,
        help="Tracks the current state of the Gemini extraction process."
    )
    gemini_extract_status = fields.Text(
        string="Gemini Extract Status",
        readonly=True,
        copy=False,
        help="Provides user-facing messages about the extraction status (e.g., errors, success)."
    )
    can_extract_with_gemini = fields.Boolean(
        compute='_compute_can_extract_with_gemini',
        string="Can Extract with Gemini",
        help="""Determines if the 'Extract with Gemini' button should be visible.
                It's visible if:
                1. The company mode is 'manual_send'.
                2. There is a main attachment.
                3. The state is 'no_extract', 'error', or 'done' (allowing for retries)."""
    )

    linkedin_profile = fields.Char(
        "LinkedIn Profile",
        tracking=True,
        copy=False,
        help="Stores the LinkedIn profile URL extracted from the CV."
    )

    @api.depends(
        'message_main_attachment_id',
        'gemini_extract_state',
        'company_id.gemini_cv_extract_mode'
    )
    def _compute_can_extract_with_gemini(self):
        """
        Computes the visibility of the 'Extract with Gemini' button.
        """
        for applicant in self:
            company = applicant.company_id or self.env.company
            is_manual_mode = company.gemini_cv_extract_mode == 'manual_send'
            # Allow extraction if not started, failed, or to re-run
            can_retry = applicant.gemini_extract_state in ('no_extract', 'error', 'done')

            applicant.can_extract_with_gemini = (
                is_manual_mode and
                applicant.message_main_attachment_id and
                can_retry
            )

    def action_extract_with_gemini(self):
        """
        Action triggered by the 'Extract with Gemini' button.
        It sets the state to 'pending', commits the change, and
        launches the extraction process in a new background thread
        to avoid blocking the UI.
        """
        applicants_to_process = self.filtered(lambda a: a.can_extract_with_gemini)
        if not applicants_to_process:
            raise UserError(_("There are no applicants here that are ready for extraction."))

        # Set state to 'pending' for instant user feedback.
        applicants_to_process.write({
            'gemini_extract_state': 'pending',
            'gemini_extract_status': _('Pending: Queued for extraction...'),
        })

        # We must commit this state change *before* the new thread starts
        # to ensure the thread sees the updated state and to prevent
        # serialization failures. This is a rare but necessary use of commit in an action.
        self.env.cr.commit()

        # Start the extraction in a separate thread to avoid UI blocking.
        thread = threading.Thread(
            target=self._run_gemini_extraction_in_thread,
            args=(applicants_to_process.ids, self.env.cr.dbname)
        )
        thread.start()

        return True  # Acknowledge the button click

    def _run_gemini_extraction_in_thread(self, applicant_ids, dbname):
        """
        Target method for the background thread.
        It creates a new, thread-safe environment and cursor
        to process the extraction logic.
        """
        try:
            # Create a new cursor for this thread
            with odoo.registry(dbname).cursor() as new_cr:
                # Create a new environment with the new cursor
                env = api.Environment(new_cr, self.env.uid, self.env.context)
                applicants_to_process = env[self._name].browse(applicant_ids)
                applicants_to_process._run_gemini_extraction()
        except Exception as e:
            _logger.error(
                "Gemini extraction thread failed: %s",
                str(e),
                exc_info=True
            )
            # If the whole thread fails, mark all records as error
            try:
                with odoo.registry(dbname).cursor() as error_cr:
                    env = api.Environment(error_cr, self.env.uid, self.env.context)
                    error_self = env[self._name].browse(applicant_ids)
                    error_self.write({
                        'gemini_extract_state': 'error',
                        'gemini_extract_status': _("Thread Error: %s", str(e)),
                    })
                    error_cr.commit()  # Commit the final error state
            except Exception as e2:
                _logger.error("Could not write thread error state to applicants: %s", str(e2))


    def _run_gemini_extraction(self):
        """
        Main extraction logic, designed to be run in a thread.
        It iterates over each applicant, calls the Gemini API,
        parses the response, and writes the data.
        Each applicant is processed in its own transaction savepoint
        to isolate failures.
        """
        for applicant in self:
            skill_status_message = _('Successfully extracted data.')
            gemini_skills_list = []

            try:
                # --- Transaction Step 1: Read, Call API, Write Simple Data ---
                # Use a savepoint to roll back this applicant's changes on failure
                # without affecting other applicants in the loop.
                with self.env.cr.savepoint():
                    _logger.info("Starting Gemini extraction for applicant ID: %s", applicant.id)
                    company = applicant.company_id or self.env.company
                    api_key = company.gemini_api_key
                    model_name = company.gemini_model or 'gemini-1.5-flash-latest'

                    # 1. Validate Configuration
                    if not api_key:
                        raise UserError(_("Gemini API Key is not set in HR Settings."))
                    if not model_name:
                        raise UserError(_("Gemini Model is not set in HR Settings."))
                    if not applicant.message_main_attachment_id:
                        raise UserError(_("No CV attached."))
                    
                    attachment = applicant.message_main_attachment_id
                    if not attachment.datas:
                        raise UserError(_("Attached CV is empty."))

                    # 2. Set state to 'processing'
                    applicant.write({
                        'gemini_extract_state': 'processing',
                        'gemini_extract_status': _('Processing: Calling Gemini API...'),
                    })

                    # 3. Prepare data for API
                    # Decode base64 data from Odoo to raw bytes for the API
                    cv_blob = {
                        'mime_type': attachment.mimetype,
                        'data': base64.b64decode(attachment.datas)
                    }

                    # 4. Configure and call the Gemini API
                    genai.configure(api_key=api_key)
                    model = genai.GenerativeModel(model_name)
                    prompt = GEMINI_CV_EXTRACTION_PROMPT_FILE
                    
                    _logger.info("Calling Gemini model '%s' for applicant %s", model_name, applicant.id)
                    response = model.generate_content([prompt, cv_blob])
                    
                    _logger.debug(
                        "Gemini Raw Response for Applicant %s:\n%s",
                        applicant.id,
                        response.text
                    )

                    # 5. Parse Response
                    applicant.write({
                        'gemini_extract_status': _('Processing: Parsing response...'),
                    })
                    extracted_data = self._parse_gemini_response(response.text)
                    _logger.info(
                        "Parsed Data for Applicant %s: \n%s",
                        applicant.id,
                        json.dumps(extracted_data, indent=2)
                    )

                    # 6. Write Simple Fields
                    applicant._write_extracted_data(extracted_data)

                    # 7. Check for skills to process
                    if (extracted_data.get('skills') and
                            self.env['ir.module.module']._get('hr_recruitment_skills').state == 'installed'):
                        gemini_skills_list = extracted_data.get('skills')

                # --- Transaction Step 2: Process Skills ---
                # This is in a separate savepoint. If it fails,
                # Step 1 (simple data) will still be committed.
                if gemini_skills_list:
                    try:
                        with self.env.cr.savepoint():
                            _logger.info("Processing %s skills for Applicant %s", len(gemini_skills_list), applicant.id)
                            applicant._process_skills(gemini_skills_list)
                    except Exception as e_skill:
                        _logger.error(
                            "Failed to process skills for Applicant %s: %s. "
                            "Simple data will be kept.",
                            applicant.id, str(e_skill), exc_info=True
                        )
                        # Update status message to reflect partial success
                        skill_status_message = _(
                            "Successfully saved simple data, "
                            "but failed to process skills: %s", str(e_skill)
                        )
                        # The savepoint automatically rolled back the failed skill transaction.

                # --- Transaction Step 3: Commit all successful changes for this applicant ---
                applicant.write({
                    'gemini_extract_state': 'done',
                    'gemini_extract_status': skill_status_message,
                })
                # This commits Step 1 and (if successful) Step 2
                self.env.cr.commit()

            except Exception as e:
                # Catch ALL errors from Step 1 (API, Parse, Write)
                _logger.error(
                    "Gemini extraction for applicant %s failed: %s",
                    applicant.id,
                    str(e),
                    exc_info=True
                )
                # Rollback any partial changes from the failed transaction
                self.env.cr.rollback()

                # Use a new cursor to write the error state,
                # as the current one might be in a failed state.
                try:
                    with odoo.registry(self.env.cr.dbname).cursor() as error_cr:
                        env = api.Environment(error_cr, self.env.uid, self.env.context)
                        applicant_rec = env[self._name].browse(applicant.id)
                        applicant_rec.write({
                            'gemini_extract_state': 'error',
                            'gemini_extract_status': _("Error: %s", str(e)),
                        })
                        error_cr.commit()
                except Exception as e2:
                    _logger.error("Could not write error state to applicant %s: %s", applicant.id, str(e2))
                    self.env.cr.rollback()

    def _parse_gemini_response(self, response_text):
        """
        Cleans and parses the text response from Gemini,
        expecting it to be a JSON object.
        This is made robust to handle markdown fences (```json)
        or other surrounding text.
        """
        try:
            # First, try to find a JSON block fenced by ```json ... ```
            match = re.search(r"```json\n(.*?)\n```", response_text, re.DOTALL)
            if match:
                json_text = match.group(1)
            else:
                # If no fence, look for the first { and last }
                match = re.search(r"\{.*\}", response_text, re.DOTALL)
                if match:
                    json_text = match.group(0)
                else:
                    _logger.error(
                        "No JSON object found in Gemini response for applicant %s. Raw text: %s",
                        self.id,
                        response_text
                    )
                    raise json.JSONDecodeError("No JSON object found in response.", response_text, 0)

            json_text = json_text.strip()
            return json.loads(json_text)

        except json.JSONDecodeError as e:
            _logger.error(
                "Gemini response was not valid JSON for applicant %s: %s. Raw text: %s",
                self.id,
                str(e),
                response_text
            )
            raise UserError(_(
                "Gemini returned an invalid response that could not be parsed. Raw text: %s",
                response_text
            ))

    def _write_extracted_data(self, data):
        """
        Writes the extracted simple fields (name, email, etc.)
        from the JSON data to the applicant record.
        This will overwrite existing data if new data is found.
        """
        self.ensure_one()
        if not data:
            _logger.warning("No data found to write for applicant %s.", self.id)
            return

        write_vals = {}

        if data.get('name'):
            write_vals['partner_name'] = data['name']
            # Also set the main 'name' if it's the default
            applicant_name = self.name or ''
            if not self.name or applicant_name.endswith("'s Application"):
                 write_vals['name'] = _("%s's Application") % data['name']

        if data.get('email'):
            write_vals['email_from'] = data['email']

        if data.get('phone'):
            write_vals['partner_phone'] = data['phone']

        if data.get('linkedin'):
            write_vals['linkedin_profile'] = data['linkedin']

        # Write to Odoo's standard 'type_id' (Degree) field
        if data.get('degree'):
            degree_name = data.get('degree')
            if degree_name:
                degree_env = self.env['hr.recruitment.degree']
                # Find existing degree (case-insensitive)
                degree_rec = degree_env.search([('name', '=ilike', degree_name)], limit=1)
                if not degree_rec:
                    _logger.info("Creating new degree: %s", degree_name)
                    try:
                        # Create if not found
                        degree_rec = degree_env.create({'name': degree_name})
                    except Exception as e:
                        _logger.error("Failed to create degree '%s': %s", degree_name, str(e))
                        # Don't block the write, just log and continue
                        pass

                if degree_rec:
                    write_vals['type_id'] = degree_rec.id

        if write_vals:
            _logger.info(
                "Writing data for Applicant %s: \n%s",
                self.id,
                json.dumps(write_vals, indent=2)
            )
            self.write(write_vals)
        else:
            _logger.info("No new simple data to write for applicant %s.", self.id)

    def _get_or_create_default_skill_level(self):
        """
        Finds or creates a 'Beginner (15%)' skill level to use as a fallback
        when a skill level is not provided or recognized.
        Returns:
            hr.skill.level: The default skill level record.
        """
        skill_level_env = self.env['hr.skill.level']
        default_name = 'Beginner'
        default_progress = 15
        
        # 1. Try to find the exact match
        default_level = skill_level_env.search([
            ('name', '=ilike', default_name),
            ('level_progress', '=', default_progress)
        ], limit=1)

        # 2. Fallback: find any "Beginner"
        if not default_level:
            default_level = skill_level_env.search([
                ('name', '=ilike', default_name)
            ], limit=1)

        # 3. Fallback: find the lowest progress level
        if not default_level:
             default_level = skill_level_env.search(
                 [('level_progress', '>', 0)],
                 order='level_progress asc',
                 limit=1
            )

        # 4. Create if it doesn't exist
        if not default_level:
            _logger.warning(
                "No 'Beginner (15%)' skill level found. Creating a new one."
            )
            try:
                default_level = skill_level_env.create({
                    'name': default_name,
                    'level_progress': default_progress
                })
            except Exception as e:
                _logger.error("Failed to create default 'Beginner (15%)' skill level: %s", str(e))
                raise UserError(_(
                    "Could not create default 'Beginner (15%)' skill level. "
                    "Please create one manually in the Skills module. Error: %s"
                ) % str(e))

        return default_level

    def _process_skills(self, gemini_skills_list):
        """
        Processes the structured skill list from Gemini:
        [ {"type": "...", "skill": "...", "level": "..."}, ... ]
        
        It finds or creates Skill Types, Skill Levels, and Skills,
        then links them to the applicant.
        
        Args:
            gemini_skills_list (list): A list of skill dictionaries from Gemini.
        """
        self.ensure_one()
        if not gemini_skills_list or not isinstance(gemini_skills_list, list):
            _logger.warning("No valid skills list found for applicant %s.", self.id)
            return

        skill_type_env = self.env['hr.skill.type']
        skill_level_env = self.env['hr.skill.level']
        skill_env = self.env['hr.skill']
        applicant_skill_env = self.env['hr.applicant.skill']

        # Cache for records found/created in this run to reduce DB queries
        type_cache = {}
        level_cache = {}
        skill_cache = {}

        # Lazy-load the default level only if needed
        default_level = None

        for skill_obj in gemini_skills_list:
            if not isinstance(skill_obj, dict):
                _logger.warning("Skipping invalid skill item (not a dict): %s", skill_obj)
                continue

            skill_name_str = skill_obj.get('skill')
            type_name_str = skill_obj.get('type') or 'General' # Default to 'General'
            level_name_str = skill_obj.get('level')

            if not skill_name_str:
                _logger.warning("Skipping skill with no name: %s", skill_obj)
                continue

            try:
                # --- 1. Find or Create Skill Type ---
                type_name_lower = type_name_str.lower()
                skill_type = type_cache.get(type_name_lower)
                if not skill_type:
                    skill_type = skill_type_env.search([('name', '=ilike', type_name_str)], limit=1)
                    if not skill_type:
                        skill_type = skill_type_env.create({'name': type_name_str})
                    type_cache[type_name_lower] = skill_type

                # --- 2. Find or Create Skill Level ---
                skill_level = None
                if level_name_str:
                    level_name_lower = level_name_str.lower()
                    skill_level = level_cache.get(level_name_lower)
                    if not skill_level:
                        # Try to parse "Name (Progress%)"
                        match = re.match(r"(.+?)\s*\((\d+)%\)", level_name_str)
                        if match:
                            level_name_clean = match.group(1).strip()
                            level_progress = int(match.group(2))
                            skill_level = skill_level_env.search([
                                ('name', '=ilike', level_name_clean),
                                ('level_progress', '=', level_progress)
                            ], limit=1)
                        
                        # Fallback: search by name only
                        if not skill_level:
                            skill_level = skill_level_env.search([('name', '=ilike', level_name_str)], limit=1)
                        
                        # Fallback: create it if we have parsed info
                        if not skill_level and match:
                            skill_level = skill_level_env.create({
                                'name': level_name_clean,
                                'level_progress': level_progress
                            })

                        if skill_level:
                            level_cache[level_name_lower] = skill_level

                # If no level found/created after all checks, get the default
                if not skill_level:
                    if not default_level:  # Lazy-load
                        default_level = self._get_or_create_default_skill_level()
                    skill_level = default_level

                # --- 3. Associate Level with Type (Fixes NOT NULL constraint) ---
                if skill_level not in skill_type.skill_level_ids:
                    skill_type.write({'skill_level_ids': [(4, skill_level.id)]})

                # --- 4. Find or Create Skill ---
                skill_name_lower = skill_name_str.lower()
                skill = skill_cache.get(skill_name_lower)
                if not skill:
                    skill = skill_env.search([('name', '=ilike', skill_name_str)], limit=1)
                    if not skill:
                        # Create new skill
                        skill = skill_env.create({
                            'name': skill_name_str,
                            'skill_type_id': skill_type.id
                        })
                    elif skill.skill_type_id != skill_type:
                        # Ensure existing skill has the correct type
                        skill.write({'skill_type_id': skill_type.id})
                    
                    skill_cache[skill_name_lower] = skill

                # --- 5. Create Applicant-Skill Link ---
                existing_link = applicant_skill_env.search([
                    ('applicant_id', '=', self.id),
                    ('skill_id', '=', skill.id)
                ], limit=1)

                if not existing_link:
                    applicant_skill_env.create({
                        'applicant_id': self.id,
                        'skill_id': skill.id,
                        'skill_level_id': skill_level.id,
                        'skill_type_id': skill_type.id,
                    })
                    _logger.info(
                        "Created link for applicant %s skill: %s (Type: %s, Level: %s)",
                        self.id, skill.name, skill_type.name, skill_level.name
                    )

            except Exception as e_item:
                _logger.error(
                    "Failed to process skill item %s for applicant %s: %s",
                    skill_obj, self.id, str(e_item), exc_info=True
                )
                # Re-raise to roll back this applicant's entire skill transaction
                raise
