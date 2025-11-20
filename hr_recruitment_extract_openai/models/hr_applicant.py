# -*- coding: utf-8 -*-
import openai
import json
import logging
import odoo
import re
from collections import defaultdict
from typing import List, Optional
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from pydantic import BaseModel, Field

from .openai_prompts import (
    OPENAI_CV_EXTRACTION_PROMPT,
    AI_MATCH_SINGLE_PROMPT_TEMPLATE,
    AI_MATCH_MULTI_PROMPT_TEMPLATE,
    AI_MATCH_MULTI_SUMMARY_PROMPT_TEMPLATE,
)

_logger = logging.getLogger(__name__)


# --- Pydantic Models for CV Extraction ---

class Skill(BaseModel):
    """Pydantic model for a single extracted skill."""
    type: str = Field(
        description="Category of the skill (e.g., 'Programming Languages')."
    )
    skill: str = Field(description="Name of the skill (e.g., 'Python').")
    level: str = Field(
        description="Proficiency level (e.g., 'Advanced (80%)'). "
                    "Use 'Beginner (15%)' if not specified."
    )

class CVExtraction(BaseModel):
    """Pydantic model for the main CV extraction."""
    name: Optional[str] = Field(
        description="Full name of the applicant.", default=None
    )
    email: Optional[str] = Field(
        description="Primary email address.", default=None
    )
    phone: Optional[str] = Field(
        description="Primary phone number.", default=None
    )
    linkedin: Optional[str] = Field(
        description="Applicant's LinkedIn profile URL.", default=None
    )
    degree: Optional[str] = Field(
        description="Highest or most relevant academic degree.", default=None
    )
    skills: List[Skill] = Field(
        description="A list of professional skills found in the CV.",
        default_factory=list
    )


# --- Pydantic Models for AI Match ---

class AIStatementMatch(BaseModel):
    """Pydantic model for a single requirement match statement."""
    requirement_id: int = Field(
        description="The ID of the requirement being evaluated."
    )
    match_fit: str = Field(
        description="The fit score (not_fit, poor_fit, fit, "
                    "good_fit, excellent_fit)."
    )
    explanation: str = Field(
        description="Concise explanation for the score, citing CV evidence."
    )

class AISummary(BaseModel):
    """Pydantic model for the overall match summary."""
    overall_fit: str = Field(
        description="Brief overall fit (e.g., 'Strong potential')."
    )
    key_strengths: List[str] = Field(
        description="List of key strengths matching requirements."
    )
    missing_gaps: List[str] = Field(
        description="List of key requirements that are missing."
    )

class AISingleMatch(BaseModel):
    """Pydantic model for the Single-Prompt AI Match response."""
    summary: AISummary
    statement_matches: List[AIStatementMatch]

class AIMultiMatch(BaseModel):
    """Pydantic model for the Multi-Prompt (Category) AI Match response."""
    statement_matches: List[AIStatementMatch]

class AIMultiSummary(BaseModel):
    """Pydantic model for the Multi-Prompt (Summary) AI Match response."""
    summary: AISummary


class HrApplicant(models.Model):
    _inherit = 'hr.applicant'
    _order = (
        "openai_extract_state desc, "
        "ai_match_state desc, "
        "priority desc, "
        "id desc"
    )

    # --- 1. Fields for CV Extraction ---
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
    )
    openai_extract_status = fields.Text(
        string="OpenAI Extract Status",
        readonly=True,
        copy=False
    )
    can_extract_with_openai = fields.Boolean(
        compute='_compute_can_extract_with_openai',
        string="Can Extract with OpenAI"
    )
    linkedin_profile = fields.Char(
        "LinkedIn Profile",
        copy=False,
        help="Stores the LinkedIn profile URL extracted from the CV."
    )

    # --- 2. Fields for AI Match ---
    ai_match_percent = fields.Float(
        string='AI Match (%)',
        compute='_compute_ai_match_percent',
        store=True,
        digits=(16, 2),
        help="Overall match score calculated from weighted requirements.",
    )
    ai_match_summary_fit = fields.Char(
        string="AI Summary: Overall Fit",
        readonly=True,
        copy=False
    )
    ai_match_summary_strengths = fields.Text(
        string="AI Summary: Key Strengths",
        readonly=True,
        copy=False
    )
    ai_match_summary_gaps = fields.Text(
        string="AI Summary: Missing Gaps",
        readonly=True,
        copy=False
    )

    ai_match_statement_ids = fields.One2many(
        'hr.applicant.match.statement',
        'applicant_id',
        string="AI Match Details"
    )
    ai_match_state = fields.Selection(
        selection=[
            ('no_match', 'Not Matched'),
            ('pending', 'Pending'),
            ('processing', 'Processing'),
            ('done', 'Done'),
            ('error', 'Error'),
        ],
        string='AI Match State',
        default='no_match',
        required=True,
        copy=False,
    )
    ai_match_status = fields.Text(
        string="AI Match Status",
        readonly=True,
        copy=False
    )

    can_run_ai_match = fields.Boolean(
        compute='_compute_can_run_ai_match',
        string="Can Run AI Match"
    )
    ai_match_mode = fields.Selection(
        related='job_id.ai_match_mode',
        string="Applicant Match Mode",
        readonly=True
    )

    # --- Compute Methods ---

    @api.depends('message_main_attachment_id',
                 'openai_extract_state',
                 'company_id.openai_cv_extract_mode')
    def _compute_can_extract_with_openai(self):
        for applicant in self:
            company = applicant.company_id or self.env.company
            is_manual_mode = company.openai_cv_extract_mode == 'manual_send'
            can_retry = applicant.openai_extract_state in (
                'no_extract', 'error', 'done'
            )
            applicant.can_extract_with_openai = (
                is_manual_mode and
                applicant.message_main_attachment_id and
                can_retry
            )

    @api.depends('job_id.requirement_statement_ids',
                 'message_main_attachment_id',
                 'ai_match_state')
    def _compute_can_run_ai_match(self):
        for applicant in self:
            has_job = applicant.job_id
            has_reqs = has_job and applicant.job_id.requirement_statement_ids
            has_cv = applicant.message_main_attachment_id
            can_retry = applicant.ai_match_state in (
                'no_match', 'error', 'done'
            )
            applicant.can_run_ai_match = (
                has_job and has_reqs and has_cv and can_retry
            )

    @api.depends('ai_match_statement_ids.match_score',
                 'ai_match_statement_ids.requirement_weight')
    def _compute_ai_match_percent(self):
        for applicant in self:
            if not applicant.ai_match_statement_ids:
                applicant.ai_match_percent = 0.0
                continue

            achieved_score = 0.0
            total_possible_weight = 0.0

            for statement in applicant.ai_match_statement_ids:
                normalized_score = statement.match_score / 100.0
                achieved_score += (
                    normalized_score * statement.requirement_weight
                )
                total_possible_weight += statement.requirement_weight

            if total_possible_weight == 0:
                applicant.ai_match_percent = 0.0
            else:
                applicant.ai_match_percent = (
                    (achieved_score / total_possible_weight) * 100.0
                )

    # --- Button Actions ---

    def action_extract_with_openai(self):
        applicants_to_process = self.filtered(
            lambda a: a.can_extract_with_openai
        )
        if not applicants_to_process:
            raise UserError(_(
                "There are no applicants here that are ready for extraction."
            ))

        applicants_to_process.write({
            'openai_extract_state': 'pending',
            'openai_extract_status': _('Pending: Queued for extraction...'),
        })

        user_id = self.env.user.id
        for applicant in applicants_to_process:
            applicant.with_delay()._run_openai_extraction_job(user_id)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Processing Started'),
                'message': _('The CV processing has been queued.'),
                'type': 'info',
                'sticky': False,
            }
        }

    def action_run_ai_match(self):
        applicants_to_process = self.filtered(lambda a: a.can_run_ai_match)
        if not applicants_to_process:
            raise UserError(_(
                "Cannot run AI Match. Ensure the applicant has a Job, "
                "a CV, and Job Requirements."
            ))

        applicants_to_process.write({
            'ai_match_state': 'pending',
            'ai_match_status': _('Pending: Queued for AI matching...'),
        })

        user_id = self.env.user.id
        for applicant in applicants_to_process:
            applicant.with_delay()._run_ai_match_job(user_id)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Processing Started'),
                'message': _('The AI matching has been queued.'),
                'type': 'info',
                'sticky': False,
            }
        }

    @api.model
    def _notify_user(self, user_id, params):
        try:
            with odoo.registry(self.env.cr.dbname).cursor() as notify_cr:
                notify_env = api.Environment(
                    notify_cr, self.env.uid, self.env.context
                )
                user = notify_env['res.users'].browse(user_id)
                if user.partner_id:
                    notify_env['bus.bus']._sendone(
                        user.partner_id, 'simple_notification', params
                    )
                notify_cr.commit()
        except Exception as e:
            _logger.error(
                "Failed to send notification to user %s: %s", user_id, str(e)
            )

    # --- Background Job: CV Extraction ---

    def _run_openai_extraction_job(self, user_id):
        self.ensure_one()
        applicant = self
        success = False
        error_message = ""

        try:
            with self.env.cr.savepoint():
                _logger.info(
                    "Starting OpenAI extraction for applicant ID: %s",
                    applicant.id
                )
                applicant.write({
                    'openai_extract_state': 'processing',
                    'openai_extract_status': _(
                        'Processing: Calling OpenAI API...'
                    ),
                })

                response_model = self.env['hr.applicant']._openai_call(
                    applicant.message_main_attachment_id,
                    prompt=OPENAI_CV_EXTRACTION_PROMPT,
                    text_format=CVExtraction
                )

                applicant.write({
                    'openai_extract_status': _(
                        'Processing: Converting response...'
                    )
                })
                extracted_data = response_model.model_dump(mode='json')

                skill_status_message = applicant._process_extracted_cv_data(
                    extracted_data
                )

            applicant.write({
                'openai_extract_state': 'done',
                'openai_extract_status': skill_status_message,
            })
            success = True

        except Exception as e:
            _logger.error(
                "OpenAI extraction failed: %s", str(e), exc_info=True
            )
            self.env.cr.rollback()
            error_message = _("Error: %s", str(e))
            applicant.write({
                'openai_extract_state': 'error',
                'openai_extract_status': error_message,
            })
            self.env.cr.commit()
            success = False

        finally:
            params = {}
            if success:
                params = {
                    'title': _('Processing Complete'),
                    'message': _("Extracted data for '%s'.", applicant.name),
                    'type': 'success'
                }
            elif error_message:
                params = {
                    'title': _('Processing Failed'),
                    'message': _(
                        "Failed to extract data.\n%s", error_message
                    ),
                    'type': 'warning',
                    'sticky': True
                }

            if params:
                self._notify_user(user_id, params)

    # --- Background Job: AI Match ---

    def _get_or_create_ai_match_tag(self, percent=None):
        """
        Helper to classify the applicant's match percentage
        into a color-coded tag (e.g. "Fit", "Good Fit").
        """
        self.ensure_one()
        ApplicantTag = self.env['hr.applicant.category']
        tag_name = "AI Match: Failed"
        tag_color = 2  # Red

        if percent is not None:
            fit_display_name = "N/A"
            if percent <= 30:
                fit_display_name, tag_color = "Not a Fit", 2
            elif percent <= 50:
                fit_display_name, tag_color = "Poor Fit", 3
            elif percent <= 70:
                fit_display_name, tag_color = "Fit", 5
            elif percent <= 90:
                fit_display_name, tag_color = "Good Fit", 10
            else:
                fit_display_name, tag_color = "Excellent Fit", 20

            tag_name = f"AI Match: {fit_display_name} - {percent:.0f}%"

        tag = ApplicantTag.search(
            [('name', '=', tag_name), ('color', '=', tag_color)], limit=1
        )
        if not tag:
            tag = ApplicantTag.create({'name': tag_name, 'color': tag_color})

        old_tags = self.categ_ids.filtered(
            lambda t: t.name.startswith('AI Match:')
        )
        commands = []
        if old_tags:
            commands.extend(
                [(3, old_tag.id) for old_tag in old_tags if old_tag.id != tag.id]
            )
        if tag.id not in self.categ_ids.ids:
            commands.append((4, tag.id))
        if commands:
            self.write({'categ_ids': commands})

    def _run_ai_match_job(self, user_id):
        self.ensure_one()
        applicant = self
        success = False
        error_message = ""

        try:
            if not applicant.job_id.requirement_statement_ids:
                raise UserError(_(
                    "No job requirements found for job '%s'.",
                    applicant.job_id.name
                ))

            if applicant.ai_match_mode == 'multi_prompt':
                self._run_ai_match_job_multi(user_id)
            else:
                self._run_ai_match_job_single(user_id)

            success = True
            applicant.write({
                'ai_match_state': 'done',
                'ai_match_status': _(
                    'AI Match complete. Score: %s%%',
                    round(applicant.ai_match_percent, 2)
                ),
            })
            applicant._get_or_create_ai_match_tag(
                percent=applicant.ai_match_percent
            )

        except Exception as e:
            _logger.error("AI Match failed: %s", str(e), exc_info=True)
            self.env.cr.rollback()
            error_message = _("Error: %s", str(e))
            applicant.write({
                'ai_match_state': 'error',
                'ai_match_status': error_message,
            })
            try:
                applicant._get_or_create_ai_match_tag(percent=None)
            except Exception:
                self.env.cr.rollback()

            self.env.cr.commit()
            success = False

        finally:
            params = {}
            if success:
                params = {
                    'title': _('AI Match Complete'),
                    'message': _(
                        "Matched CV for '%s'. Score: %s%%",
                        applicant.name, round(applicant.ai_match_percent, 2)
                    ),
                    'type': 'success'
                }
            elif error_message:
                params = {
                    'title': _('AI Match Failed'),
                    'message': _(
                        "Failed to match CV.\n%s", error_message
                    ),
                    'type': 'warning',
                    'sticky': True
                }
            if params:
                self._notify_user(user_id, params)

    def _run_ai_match_job_single(self, user_id):
        self.ensure_one()
        with self.env.cr.savepoint():
            self.write({
                'ai_match_state': 'processing',
                'ai_match_status': _(
                    'Processing (Single): Preparing requirements...'
                )
            })

            job_reqs = self.job_id.requirement_statement_ids
            job_requirements_data = [{
                'id': req.id,
                'name': req.name,
                'weight': req.weight,
                'tags': [tag.name for tag in req.tag_ids],
                'relevant_companies': [
                    p.name for p in req.company_relevance_ids
                ]
            } for req in job_reqs]

            prompt = AI_MATCH_SINGLE_PROMPT_TEMPLATE.format(
                job_requirements_json=json.dumps(
                    job_requirements_data, indent=2
                )
            )

            self.write({
                'ai_match_status': _(
                    'Processing (Single): Calling AI service...'
                )
            })
            response_model = self.env['hr.applicant']._openai_call(
                self.message_main_attachment_id,
                prompt=prompt,
                text_format=AISingleMatch
            )

            self._process_ai_match_data(response_model.model_dump(mode='json'))

    def _run_ai_match_job_multi(self, user_id):
        """
        Executes the multi-step matching process:
        1. Split requirements by category (Hard Skill, Soft Skill, etc.)
        2. Call OpenAI for each category.
        3. Aggregate results and call OpenAI for a final summary.
        """
        self.ensure_one()
        with self.env.cr.savepoint():
            self.write({
                'ai_match_state': 'processing',
                'ai_match_status': _(
                    'Processing (Multi): Preparing requirements...'
                )
            })

            all_reqs = self.job_id.requirement_statement_ids
            reqs_by_category = defaultdict(list)
            cat_keys = {
                'hard skill': 'Hard Skills',
                'soft skill': 'Soft Skills',
                'domain knowledge': 'Domain Knowledge',
                'operational': 'Operational'
            }

            for req in all_reqs:
                found = False
                for tag in req.tag_ids:
                    key = tag.name.strip().lower()
                    if key in cat_keys:
                        reqs_by_category[cat_keys[key]].append(req)
                        found = True
                        break
                if not found:
                    reqs_by_category['Operational'].append(req)

            all_statement_matches = []
            analysis_notes = []

            categories = [
                'Hard Skills',
                'Soft Skills',
                'Domain Knowledge',
                'Operational'
            ]

            for category in categories:
                reqs = reqs_by_category.get(category)
                if not reqs:
                    continue

                self.write({
                    'ai_match_status': _(
                        'Processing (Multi): Analyzing %s...', category
                    )
                })
                job_data = [{
                    'id': r.id,
                    'name': r.name,
                    'weight': r.weight,
                    'relevant_companies': [
                        p.name for p in r.company_relevance_ids
                    ]
                } for r in reqs]

                prompt = AI_MATCH_MULTI_PROMPT_TEMPLATE.format(
                    category_name=category,
                    job_requirements_json=json.dumps(job_data, indent=2)
                )
                response = self.env['hr.applicant']._openai_call(
                    self.message_main_attachment_id,
                    prompt=prompt,
                    text_format=AIMultiMatch
                )

                matches = response.model_dump(mode='json').get(
                    'statement_matches', []
                )
                all_statement_matches.extend(matches)

                for m in matches:
                    req_record = self.env['hr.job.requirement'].browse(
                        m.get('requirement_id')
                    )
                    analysis_notes.append({
                        'category': category,
                        'requirement': req_record.name,
                        'fit': m.get('match_fit'),
                        'explanation': m.get('explanation')
                    })

            if not all_statement_matches:
                raise UserError(_("Multi-Prompt analysis returned no results."))

            self.write({
                'ai_match_status': _('Processing (Multi): Generating summary...')
            })
            summary_prompt = AI_MATCH_MULTI_SUMMARY_PROMPT_TEMPLATE.format(
                analysis_notes_json=json.dumps(analysis_notes, indent=2)
            )
            summary_response = self.env['hr.applicant']._openai_call(
                self.message_main_attachment_id,
                prompt=summary_prompt,
                text_format=AIMultiSummary
            )

            final_data = {
                "summary": summary_response.model_dump(mode='json').get(
                    'summary'
                ),
                "statement_matches": all_statement_matches
            }
            self._process_ai_match_data(final_data)

    # --- Helpers ---

    @api.model
    def _openai_get_client(self, company_id=None):
        company = (
            self.env['res.company'].browse(company_id)
            if company_id else self.env.company
        )
        api_key = (company.openai_api_key or '').strip()
        model = (company.openai_model or 'gpt-4o').strip()
        if not api_key:
            raise UserError(_("OpenAI API Key is not set."))
        if not model:
            raise UserError(_("OpenAI Model is not set."))
        return openai.OpenAI(api_key=api_key), model

    @api.model
    def _openai_call(self, attachment, prompt, text_format):
        company = attachment.company_id or self.env.company
        client, model_name = self._openai_get_client(company.id)

        if not attachment or not attachment.datas:
            raise UserError(_("CV is empty: %s", attachment.name))

        base64_string = attachment.datas.decode('utf-8')
        file_data_uri = f"data:{attachment.mimetype};base64,{base64_string}"

        user_content = [
            {
                "type": "input_file",
                "filename": attachment.name,
                "file_data": file_data_uri
            },
            {"type": "input_text", "text": "Analyze the attached file."}
        ]

        try:
            _logger.info(
                "Calling OpenAI (parse) model '%s' for %s",
                model_name, attachment.name
            )
            response = client.responses.parse(
                model=model_name,
                input=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_content}
                ],
                text_format=text_format,
                temperature=0,
            )
            return response.output[0].content[0].parsed
        except Exception as e:
            _logger.error("OpenAI API call failed: %s", str(e), exc_info=True)
            raise UserError(_("OpenAI API call failed: %s", str(e)))

    def _process_extracted_cv_data(self, extracted_data):
        self.ensure_one()
        status = _('Successfully extracted data.')
        skills_list = []

        try:
            with self.env.cr.savepoint():
                self._write_extracted_data(extracted_data)
                if (extracted_data.get('skills') and
                        self.env['ir.module.module']._get(
                            'hr_recruitment_skills'
                        ).state == 'installed'):
                    skills_list = extracted_data.get('skills')
        except Exception as e:
            _logger.error("Failed to write simple data: %s", str(e))
            raise UserError(_("Failed to write simple data: %s", str(e)))

        if skills_list:
            try:
                with self.env.cr.savepoint():
                    self._process_skills(skills_list)
            except Exception as e:
                _logger.error("Failed to process skills: %s", str(e))
                status = _("Saved simple data, but failed skills: %s", str(e))

        return status

    def _write_extracted_data(self, data):
        self.ensure_one()
        if not data:
            return
        vals = {}
        if data.get('name'):
            vals['partner_name'] = data['name']
            if not self.name or self.name.endswith("'s Application"):
                vals['name'] = _("%s's Application") % data['name']
        if data.get('email'):
            vals['email_from'] = data['email']
        if data.get('phone'):
            vals['partner_phone'] = data['phone']
        if data.get('linkedin'):
            match = re.search(
                r'(https?://(?:www\.)?linkedin\.com/[^\s)\]]+)',
                str(data['linkedin'])
            )
            vals['linkedin_profile'] = (
                match.group(1) if match else str(data['linkedin']).strip()
            )

        if data.get('degree'):
            deg_name = data['degree']
            deg_env = self.env['hr.recruitment.degree']
            deg = deg_env.search([('name', '=ilike', deg_name)], limit=1)
            if not deg:
                try:
                    deg = deg_env.create({'name': deg_name})
                except Exception:
                    pass
            if deg:
                vals['type_id'] = deg.id

        if vals:
            self.write(vals)

    def _process_skills(self, skills_list):
        self.ensure_one()
        skill_type_env = self.env['hr.skill.type']
        skill_level_env = self.env['hr.skill.level']
        skill_env = self.env['hr.skill']

        default_level = None

        for item in skills_list:
            if not isinstance(item, dict):
                continue
            s_name = item.get('skill')
            s_type = item.get('type', 'General')
            s_level = item.get('level')
            if not s_name:
                continue

            try:
                st = skill_type_env.search(
                    [('name', '=ilike', s_type)], limit=1
                ) or skill_type_env.create({'name': s_type})

                sl = None
                if s_level:
                    match = re.match(r"(.+?)\s*\((\d+)%\)", s_level)
                    if match:
                        sl = skill_level_env.search([
                            ('name', '=ilike', match.group(1).strip()),
                            ('level_progress', '=', int(match.group(2)))
                        ], limit=1)
                        if not sl:
                            sl = skill_level_env.create({
                                'name': match.group(1).strip(),
                                'level_progress': int(match.group(2))
                            })
                    if not sl:
                        sl = skill_level_env.search(
                            [('name', '=ilike', s_level)], limit=1
                        )

                if not sl:
                    if not default_level:
                        default_level = skill_level_env.search(
                            [('name', '=ilike', 'Beginner')], limit=1
                        ) or skill_level_env.create({
                            'name': 'Beginner', 'level_progress': 15
                        })
                    sl = default_level

                if sl not in st.skill_level_ids:
                    st.write({'skill_level_ids': [(4, sl.id)]})

                sk = skill_env.search(
                    [('name', '=ilike', s_name), ('skill_type_id', '=', st.id)],
                    limit=1
                )
                if not sk:
                    sk = skill_env.search(
                        [('name', '=ilike', s_name)], limit=1
                    )
                    if sk:
                        sk.write({'skill_type_id': st.id})
                    else:
                        sk = skill_env.create({
                            'name': s_name,
                            'skill_type_id': st.id
                        })

                if not self.env['hr.applicant.skill'].search([
                    ('applicant_id', '=', self.id),
                    ('skill_id', '=', sk.id)
                ], limit=1):
                    self.env['hr.applicant.skill'].create({
                        'applicant_id': self.id,
                        'skill_id': sk.id,
                        'skill_level_id': sl.id,
                        'skill_type_id': st.id
                    })

            except Exception:
                pass

    def _process_ai_match_data(self, match_data):
        self.ensure_one()
        self.ai_match_statement_ids.unlink()

        summary = match_data.get('summary', {})
        vals = {
            'ai_match_summary_fit': summary.get('overall_fit', 'N/A'),
            'ai_match_summary_strengths': "\n".join(
                f"- {s}" for s in summary.get('key_strengths', [])
            ),
            'ai_match_summary_gaps': "\n".join(
                f"- {g}" for g in summary.get('missing_gaps', [])
            ),
        }

        score_map = {
            'not_fit': 0.0,
            'poor_fit': 25.0,
            'fit': 50.0,
            'good_fit': 80.0,
            'excellent_fit': 100.0
        }
        valid_req_ids = set(self.job_id.requirement_statement_ids.ids)
        stmts = []

        for m in match_data.get('statement_matches', []):
            rid = m.get('requirement_id')
            if rid in valid_req_ids:
                fit = m.get('match_fit', 'not_fit')
                stmts.append((0, 0, {
                    'applicant_id': self.id,
                    'requirement_id': rid,
                    'explanation': m.get('explanation', ''),
                    'match_fit': fit,
                    'match_score': score_map.get(fit, 0.0)
                }))

        if stmts:
            vals['ai_match_statement_ids'] = stmts
        self.write(vals)