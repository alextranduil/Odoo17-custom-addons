# -*- coding: utf-8 -*-
import base64
import openai
import json
import logging
import odoo
import re
import threading

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# This system prompt instructs the OpenAI model to act as an HR assistant
# and extract specific fields from a CV file, returning them in a
# structured JSON format.
OPENAI_CV_EXTRACTION_PROMPT = """
You are an expert HR assistant. Your task is to extract key information from the
user-provided curriculum vitae (CV) file and return it as a valid JSON object.

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
2.  If a value is not found, return `null` for that field, except for the "skills" field.
3.  For the "skills" field, if a level is not specified, return "Beginner (15%)".
4.  The "skills" field must be a list of objects, like:
    "skills": [
      { "type": "Programming Languages", "skill": "Python", "level": "Advanced (80%)" },
      { "type": "Languages", "skill": "English", "level": "C1 (85%)" }
    ]
5. Skill levels for different type:
  - "Programming Languages": "Beginner (15%)", "Elementary (25%)", "Intermediate (50%)", "Advanced (80%)", "Expert (100%)";
  - "Languages": "C2 (100%)", "C1 (85%)", "B2 (75%)", "B1 (60%)", "A2 (40%)", "A1 (10%)";
  - "IT": "Beginner (15%)", "Elementary (25%)", "Intermediate (50%)", "Advanced (80%)", "Expert (100%)";
  - "Soft Skills": "Beginner (15%)", "Elementary (25%)", "Intermediate (50%)", "Advanced (80%)", "Expert (100%)";
  - "Marketing": (L4 (100%), L3 (75%), L2 (50%), L1 (25%)).

You must return the data in JSON format.
"""


class HrApplicant(models.Model):
    """
    Inherits `hr.applicant` to add functionality for extracting CV data
    using the OpenAI API. Manages the extraction state, button
    visibility, and processes the data received from the API.
    """
    _inherit = 'hr.applicant'
    # Sort by state to show processing/pending records first
    _order = "openai_extract_state desc, priority desc, id desc"

    openai_extract_state = fields.Selection(
        selection=[
            ('no_extract', 'No Extraction'),
            ('pending', 'Pending'),
            ('processing', 'Processing'),
            ('done', 'Done'),
            ('error', 'Error'),
        ],
        string='OpenAI Extract State',
        default='no_extract',
        required=True,
        copy=False,
        help="Tracks the current state of the OpenAI extraction process."
    )
    openai_extract_status = fields.Text(
        string="OpenAI Extract Status",
        readonly=True,
        copy=False,
        help="Provides user-facing messages about the extraction status (e.g., errors, success)."
    )
    can_extract_with_openai = fields.Boolean(
        compute='_compute_can_extract_with_openai',
        string="Can Extract with OpenAI",
        help="""Determines if the 'Extract with OpenAI' button should be visible.
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
        'openai_extract_state',
        'company_id.openai_cv_extract_mode'
    )
    def _compute_can_extract_with_openai(self):
        """
        Computes the visibility of the 'Extract with OpenAI' button.
        """
        for applicant in self:
            company = applicant.company_id or self.env.company
            is_manual_mode = company.openai_cv_extract_mode == 'manual_send'
            # Allow extraction if not started, failed, or to re-run
            can_retry = applicant.openai_extract_state in ('no_extract', 'error', 'done')

            applicant.can_extract_with_openai = (
                is_manual_mode and
                applicant.message_main_attachment_id and
                can_retry
            )

    def action_extract_with_openai(self):
        """
        Action triggered by the 'Extract with OpenAI' button.
        It sets the state to 'pending', commits the change, and
        launches the extraction process in a new background thread
        to avoid blocking the UI.
        """
        applicants_to_process = self.filtered(lambda a: a.can_extract_with_openai)
        if not applicants_to_process:
            raise UserError(_("There are no applicants here that are ready for extraction."))

        # Set state to 'pending' for instant user feedback.
        applicants_to_process.write({
            'openai_extract_state': 'pending',
            'openai_extract_status': _('Pending: Queued for extraction...'),
        })

        # We must commit this state change *before* the new thread starts
        # to ensure the thread sees the updated state and to prevent
        # serialization failures. This is a rare but necessary use of commit in an action.
        self.env.cr.commit()

        # Start the extraction in a separate thread to avoid UI blocking.
        thread = threading.Thread(
            target=self._run_openai_extraction_in_thread,
            args=(applicants_to_process.ids, self.env.cr.dbname)
        )
        thread.start()

        return True  # Acknowledge the button click

    def _run_openai_extraction_in_thread(self, applicant_ids, dbname):
        """
        Target method for the background thread.
        It creates a new, thread-safe environment and cursor
        to process the extraction logic.

        Args:
            applicant_ids (list): List of IDs for `hr.applicant` records to process.
            dbname (str): The name of the Odoo database to connect to.
        """
        try:
            # Create a new cursor for this thread, as the original
            # cursor from the web request cannot be shared.
            with odoo.registry(dbname).cursor() as new_cr:
                # Create a new environment with the new cursor
                env = api.Environment(new_cr, self.env.uid, self.env.context)
                applicants_to_process = env[self._name].browse(applicant_ids)
                applicants_to_process._run_openai_extraction()
        except Exception as e:
            _logger.error(
                "OpenAI extraction thread failed: %s",
                str(e),
                exc_info=True
            )
            # If the whole thread fails (e.g., cannot connect to DB),
            # log the error and mark all associated applicants as failed.
            try:
                with odoo.registry(dbname).cursor() as error_cr:
                    env = api.Environment(error_cr, self.env.uid, self.env.context)
                    error_self = env[self._name].browse(applicant_ids)
                    error_self.write({
                        'openai_extract_state': 'error',
                        'openai_extract_status': _("Thread Error: %s", str(e)),
                    })
                    error_cr.commit()  # Commit the final error state
            except Exception as e2:
                _logger.error("Could not write thread error state to applicants: %s", str(e2))

    # --- REFACTORED: Core logic split into reusable @api.model methods ---

    @api.model
    def _openai_get_client(self, company_id=None):
        """
        Reusable helper to get configuration and the OpenAI client.
        This can be called by other models.

        Args:
            company_id (int, optional): The ID of the company to get settings from.
                                        If None, falls back to self.env.company.

        Returns:
            (openai.OpenAI, str): A tuple of the (client, model_name).
        """
        # In a @api.model method, self is the model/environment
        if company_id:
            company = self.env['res.company'].browse(company_id)
        else:
            company = self.env.company

        # Strip whitespace from config fields to prevent UnicodeErrors
        api_key = (company.openai_api_key or '').strip()
        model_name = (company.openai_model or 'gpt-4o-mini').strip()

        # 1. Validate Configuration
        if not api_key:
            raise UserError(_("OpenAI API Key is not set in HR Settings (or is invalid after stripping whitespace)."))
        if not model_name:
            raise UserError(_("OpenAI Model is not set in HR Settings (or is invalid after stripping whitespace)."))
        
        return openai.OpenAI(api_key=api_key), model_name

    @api.model
    def _openai_call_for_cv(self, attachment):
        """
        Reusable method to call the OpenAI API for a single CV attachment.
        This is a @api.model method and can be called from any model via
        self.env['hr.applicant']._openai_call_for_cv(att)

        Args:
            attachment (ir.attachment): The attachment record to process.

        Returns:
            str: The raw text response from the OpenAI API.
        """
        # 1. Get client and config
        # Get company from attachment first, or fall back to env company
        company = attachment.company_id or self.env.company
        client, model_name = self._openai_get_client(company.id)

        # 2. Validate attachment
        if not attachment:
            raise UserError(_("No attachment provided."))
        if not attachment.datas:
            raise UserError(_("Attached CV is empty: %s", attachment.name))

        _logger.info("Starting OpenAI call for attachment: %s", attachment.name)

        # 3. Prepare data and client
        # We send the raw base64 data directly from Odoo
        base64_string = attachment.datas.decode('utf-8')
        file_data_uri = f"data:{attachment.mimetype};base64,{base64_string}"

        # 4. Build the input payload
        user_content = [
            {
                "type": "input_file",
                "filename": attachment.name,
                "file_data": file_data_uri,
            },
            {
                "type": "input_text",
                "text": "Please extract the data from the attached CV file and return it as JSON.",
            },
        ]

        # 5. Configure and call the OpenAI API using client.responses.create()
        try:
            _logger.info("Calling OpenAI model '%s' for attachment %s using client.responses.create", model_name, attachment.name)

            response = client.responses.create(
                model=model_name,
                input=[
                    {
                        "role": "system",
                        "content": OPENAI_CV_EXTRACTION_PROMPT
                    },
                    {
                        "role": "user",
                        "content": user_content
                    }
                ],
                temperature=0, # Use 0 for deterministic JSON output
            )
            
            # As per docs, output is in response.output_text
            response_text = response.output_text

        except Exception as e:
            _logger.error("OpenAI API call failed: %s", str(e), exc_info=True)
            raise UserError(_("OpenAI API call failed: %s", str(e)))

        _logger.debug(
            "OpenAI Raw Response for attachment %s:\n%s",
            attachment.name,
            response_text
        )
        return response_text

    @api.model
    def _parse_openai_response(self, response_text, record_id=None):
        """
        Cleans and parses the text response from OpenAI.
        This is a @api.model method.

        Args:
            response_text (str): The raw text from OpenAI.
            record_id (str, optional): A reference ID for logging (e.g., applicant ID or job ID).
        """
        log_id = record_id or 'unknown'
        try:
            # First, try to parse directly.
            try:
                return json.loads(response_text)
            except json.JSONDecodeError:
                _logger.warning("Direct JSON parsing failed for %s, checking for fences.", log_id)
                # Fallback to regex search if direct parsing fails
                pass

            # Fallback 1: find json block
            match = re.search(r"```json\n(.*?)\n```", response_text, re.DOTALL)
            if match:
                json_text = match.group(1)
            else:
                # Fallback 2: look for { ... }
                match = re.search(r"\{.*\}", response_text, re.DOTALL)
                if match:
                    json_text = match.group(0)
                else:
                    _logger.error(
                        "No JSON object found in OpenAI response for %s. Raw text: %s",
                        log_id,
                        response_text
                    )
                    raise json.JSONDecodeError("No JSON object found in response.", response_text, 0)

            json_text = json_text.strip()
            return json.loads(json_text)

        except json.JSONDecodeError as e:
            _logger.error(
                "OpenAI response was not valid JSON for %s: %s. Raw text: %s",
                log_id,
                str(e),
                response_text
            )
            raise UserError(_(
                "OpenAI returned an invalid response that could not be parsed. Raw text: %s",
                response_text
            ))
            
    def _process_extracted_cv_data(self, extracted_data):
        """
        Reusable method to write all extracted data (simple fields and skills)
        to `self` (an hr.applicant record).
        
        This is an INSTANCE method, as it operates on a specific applicant.
        
        Args:
            extracted_data (dict): The parsed JSON data from OpenAI.
            
        Returns:
            str: A status message for the operation.
        """
        self.ensure_one()
        skill_status_message = _('Successfully extracted data.')
        openai_skills_list = []

        # --- Transaction Step 1: Process Simple Data ---
        try:
            with self.env.cr.savepoint():
                self._write_extracted_data(extracted_data)
                
                # Check for skills to process
                if (extracted_data.get('skills') and
                        self.env['ir.module.module']._get('hr_recruitment_skills').state == 'installed'):
                    openai_skills_list = extracted_data.get('skills')
        except Exception as e_simple:
            _logger.error(
                "Failed to write simple data for Applicant %s: %s.",
                self.id, str(e_simple), exc_info=True
            )
            # Re-raise to stop processing; the caller will handle the rollback
            raise UserError(_("Failed to write simple data: %s") % str(e_simple))


        # --- Transaction Step 2: Process Skills ---
        if openai_skills_list:
            try:
                with self.env.cr.savepoint():
                    _logger.info("Processing %s skills for Applicant %s", len(openai_skills_list), self.id)
                    self._process_skills(openai_skills_list)
            except Exception as e_skill:
                _logger.error(
                    "Failed to process skills for Applicant %s: %s. "
                    "Simple data will be kept.",
                    self.id, str(e_skill), exc_info=True
                )
                # Update status message to reflect partial success
                skill_status_message = _(
                    "Successfully saved simple data, "
                    "but failed to process skills: %s", str(e_skill)
                )
                # The savepoint automatically rolled back the failed skill transaction.

        return skill_status_message


    def _run_openai_extraction(self):
        """
        Main extraction logic, designed to be run in a thread.
        This is now a simple orchestrator for the reusable methods.
        """
        for applicant in self:
            try:
                # --- Transaction Step 1: Call API and Process Data ---
                with self.env.cr.savepoint():
                    _logger.info("Starting OpenAI extraction for applicant ID: %s", applicant.id)
                    
                    # 1. Set state to 'processing'
                    applicant.write({
                        'openai_extract_state': 'processing',
                        'openai_extract_status': _('Processing: Calling OpenAI API...'),
                    })

                    # 2. Call API (Reusable @api.model method)
                    response_text = self.env['hr.applicant']._openai_call_for_cv(applicant.message_main_attachment_id)

                    # 3. Parse Response (Reusable @api.model method)
                    applicant.write({
                        'openai_extract_status': _('Processing: Parsing response...'),
                    })
                    extracted_data = self.env['hr.applicant']._parse_openai_response(
                        response_text,
                        record_id=f"applicant_{applicant.id}"
                    )
                    _logger.info(
                        "Parsed Data for Applicant %s: \n%s",
                        applicant.id,
                        json.dumps(extracted_data, indent=2)
                    )

                    # 4. Write all data (Reusable INSTANCE method)
                    skill_status_message = applicant._process_extracted_cv_data(extracted_data)

                # --- Transaction Step 2: Commit all successful changes for this applicant ---
                applicant.write({
                    'openai_extract_state': 'done',
                    'openai_extract_status': skill_status_message,
                })
                # This commits Step 1 (API call, data processing)
                self.env.cr.commit()

            except Exception as e:
                # Catch ALL errors from Step 1 (API, Parse, Write)
                _logger.error(
                    "OpenAI extraction for applicant %s failed: %s",
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
                            'openai_extract_state': 'error',
                            'openai_extract_status': _("Error: %s", str(e)),
                        })
                        error_cr.commit()
                except Exception as e2:
                    _logger.error("Could not write error state to applicant %s: %s", applicant.id, str(e2))
                    self.env.cr.rollback()

    # --- DATA WRITING METHODS (Unchanged, but now called by _process_extracted_cv_data) ---

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
            linkedin_url = data['linkedin']
            # Use regex to find a URL, even if it's in markdown [text](url)
            # This makes the import robust against markdown in the AI's response
            match = re.search(r'(https?://[^\s)]+)', linkedin_url)
            if match:
                write_vals['linkedin_profile'] = match.group(1)
            else:
                # Fallback if no URL found but field has non-URL text
                write_vals['linkedin_profile'] = linkedin_url

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
                        # Create if not found, but don't fail the whole
                        # transaction if this one create fails.
                        degree_rec = degree_env.create({'name': degree_name})
                    except Exception as e:
                        _logger.error("Failed to create degree '%s': %s", degree_name, str(e))
                        pass  # Log and continue

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
        
        This uses a multi-step fallback to be robust:
        1. Try to find the exact match (Name + Progress).
        2. Fallback to finding by name only.
        3. Fallback to finding the lowest progress level > 0.
        4. Fallback to creating the level.

        Returns:
            hr.skill.level: The default skill level record.
        """
        self.ensure_one() # This is an instance method, so self is an applicant
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

    def _process_skills(self, skills_list):
        """
        Processes the structured skill list from OpenAI:
        [ {"type": "...", "skill": "...", "level": "..."}, ... ]
        
        It finds or creates Skill Types, Skill Levels, and Skills,
        then links them to the applicant.
        
        This method uses caches to reduce database queries within the loop.
        
        Args:
            skills_list (list): A list of skill dictionaries from OpenAI.
        """
        self.ensure_one()
        if not skills_list or not isinstance(skills_list, list):
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

        for skill_obj in skills_list:
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
                # This is required by the `hr_recruitment_skills` module
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
                # The outer savepoint in _run_openai_extraction will catch this.
                raise