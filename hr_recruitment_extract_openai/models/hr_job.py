# -*- coding: utf-8 -*-
import logging
import odoo
from psycopg2.errors import LockNotAvailable
from typing import List
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from pydantic import BaseModel, Field

# Relative imports
from .openai_prompts import (
    JD_EXTRACT_SINGLE_PROMPT,
    OPENAI_CV_EXTRACTION_PROMPT,
)
from .hr_applicant import CVExtraction

_logger = logging.getLogger(__name__)


# --- Pydantic Models for JD Extraction ---

class JDRequirement(BaseModel):
    """Pydantic model for a single job requirement."""
    name: str = Field(
        description="The specific, measurable requirement."
    )
    weight: float = Field(
        description="Importance weight from 1.0 (low) to 10.0 (critical)."
    )
    tag_name: str = Field(
        description="Category: 'Hard Skill', 'Soft Skill', "
                    "'Domain Knowledge', or 'Operational'."
    )


class JDRequirementList(BaseModel):
    """Pydantic model to wrap the list of requirements."""
    requirements: List[JDRequirement] = Field(
        description="A list of all extracted job requirements."
    )


class HrJob(models.Model):
    _inherit = 'hr.job'

    # --- 1. Fields for Bulk CV Upload ---

    cv_attachment_ids = fields.Many2many(
        'ir.attachment',
        'hr_job_cv_attachment_rel',
        'job_id',
        'attachment_id',
        string='CVs to Process',
        help="Upload multiple CVs here to create applicants in bulk."
    )
    processed_cv_attachment_ids = fields.Many2many(
        'ir.attachment',
        'hr_job_cv_attachment_processed_rel',
        'job_id',
        'attachment_id',
        string='Successfully Processed CVs',
        readonly=True,
        copy=False,
        help="CVs that have been successfully processed and "
             "had an applicant created."
    )
    processed_cv_count = fields.Integer(
        string="Processed Count",
        readonly=True,
        copy=False,
        default=0,
        help="Number of CVs successfully processed in the last run."
    )
    failed_cv_count = fields.Integer(
        string="Failed Count",
        readonly=True,
        copy=False,
        default=0,
        help="Number of CVs that failed processing in the last run."
    )
    total_cv_count = fields.Integer(
        string="Total to Process",
        readonly=True,
        copy=False,
        default=0,
        help="Total CVs in the last processing run."
    )

    run_ai_match_on_bulk = fields.Boolean(
        string="Run AI Match",
        default=False,
        help="If selected, the system will automatically run the AI Match "
             "process for each applicant immediately after their CV data "
             "is extracted."
    )

    # State machine for Bulk CV Upload
    bulk_queue_job_uuid = fields.Char(
        string="Bulk CV Job UUID",
        copy=False,
        readonly=True
    )
    bulk_job_state = fields.Selection(
        [
            ('pending', 'Pending'),
            ('enqueued', 'Enqueued'),
            ('started', 'Started'),
            ('done', 'Done'),
            ('failed', 'Failed')
        ],
        string='Bulk Job State',
        compute='_compute_bulk_job_state',
        store=False,
        readonly=True
    )
    bulk_processing_in_progress = fields.Boolean(
        string="Bulk CV Processing",
        compute="_compute_bulk_processing_in_progress",
        help="Indicates that CVs are currently being processed "
             "in the background."
    )
    bulk_processing_complete = fields.Boolean(
        string="Bulk Processing Complete",
        default=False,
        copy=False,
        help="Indicates that a bulk CV processing has been completed."
    )
    bulk_processing_failed = fields.Boolean(
        string="Bulk Processing Failed",
        default=False,
        copy=False,
        help="Indicates that one or more CVs failed during the last "
             "bulk processing run."
    )
    bulk_processing_progress = fields.Integer(
        string="Bulk Progress",
        compute='_compute_bulk_processing_progress',
        help="Percentage of CVs processed."
    )

    # --- 2. Fields for JD Parsing & AI Match ---

    job_description_attachment_ids = fields.Many2many(
        'ir.attachment',
        'hr_job_jd_attachment_rel',
        'job_id',
        'attachment_id',
        string="Job Description File",
        help="Upload the official Job Description file (PDF, DOCX, TXT) here."
    )

    ai_match_mode = fields.Selection(
        selection=[
            ('single_prompt', 'Single Prompt (Fast, Good Overview)'),
            ('multi_prompt', 'Multi-Prompt (Slow, Detailed Analysis)'),
        ],
        string="Applicant Match Mode",
        default='single_prompt',
        required=True,
        help="Choose the AI method for matching applicants to this job:\n"
             "- Single Prompt: Uses one large AI call to analyze the CV "
             "against all requirements. Faster and provides a good general "
             "summary.\n"
             "- Multi-Prompt: Uses separate AI calls per category. Slower, "
             "but provides detailed analysis."
    )

    jd_extract_state = fields.Selection(
        selection=[
            ('no_extract', 'Not Extracted'),
            ('pending', 'Pending'),
            ('processing', 'Processing'),
            ('done', 'Done'),
            ('error', 'Error'),
        ],
        string='JD Extract State',
        default='no_extract',
        required=True,
        copy=False,
    )
    jd_extract_status = fields.Text(
        string="JD Extract Status",
        readonly=True,
        copy=False
    )
    jd_processed_attachment_ids = fields.Many2many(
        'ir.attachment',
        'hr_job_jd_processed_attachment_rel',
        'job_id',
        'attachment_id',
        string="Processed JD File",
        copy=False,
        readonly=True,
        help="Tracks the last successfully processed Job Description file."
    )

    # State machine for JD Parsing
    jd_queue_job_uuid = fields.Char(
        string="JD Job UUID",
        copy=False,
        readonly=True
    )
    jd_job_state = fields.Selection(
        [
            ('pending', 'Pending'),
            ('enqueued', 'Enqueued'),
            ('started', 'Started'),
            ('done', 'Done'),
            ('failed', 'Failed')
        ],
        string='JD Job State',
        compute='_compute_jd_job_state',
        store=False,
        readonly=True
    )
    jd_processing_in_progress = fields.Boolean(
        string="JD Processing In Progress",
        compute="_compute_jd_processing_in_progress",
    )

    # Requirement statements generated by AI
    requirement_statement_ids = fields.One2many(
        'hr.job.requirement',
        'job_id',
        string='Job Requirement Statements'
    )

    # --- Compute Methods for Bulk CV Upload ---

    @api.depends('processed_cv_count', 'failed_cv_count', 'total_cv_count')
    def _compute_bulk_processing_progress(self):
        for job in self:
            if job.total_cv_count > 0:
                total_finished = job.processed_cv_count + job.failed_cv_count
                job.bulk_processing_progress = (
                    (total_finished * 100) / job.total_cv_count
                )
            else:
                job.bulk_processing_progress = 0

    @api.depends('bulk_queue_job_uuid')
    def _compute_bulk_job_state(self):
        for job in self:
            if job.bulk_queue_job_uuid:
                job_record = self.env['queue.job'].sudo().search(
                    [('uuid', '=', job.bulk_queue_job_uuid)], limit=1
                )
                job.bulk_job_state = job_record.state if job_record else False
            else:
                job.bulk_job_state = False

    @api.depends('bulk_job_state')
    def _compute_bulk_processing_in_progress(self):
        for job in self:
            states = ('pending', 'enqueued', 'started')
            job.bulk_processing_in_progress = job.bulk_job_state in states

    # --- Compute Methods for JD Parsing ---

    @api.depends('jd_queue_job_uuid')
    def _compute_jd_job_state(self):
        for job in self:
            if job.jd_queue_job_uuid:
                job_record = self.env['queue.job'].sudo().search(
                    [('uuid', '=', job.jd_queue_job_uuid)], limit=1
                )
                job.jd_job_state = job_record.state if job_record else False
            else:
                job.jd_job_state = False

    @api.depends('jd_job_state')
    def _compute_jd_processing_in_progress(self):
        for job in self:
            states = ('pending', 'enqueued', 'started')
            job.jd_processing_in_progress = job.jd_job_state in states

    # --- Actions for Bulk CV Upload ---

    def action_process_cvs(self):
        """
        Triggered by the 'Add Candidates from CVs' button.
        Launches one background job to manage the processing.
        """
        self.ensure_one()
        try:
            # Use FOR UPDATE NOWAIT to prevent concurrent processing
            self.env.cr.execute(
                'SELECT id FROM hr_job WHERE id = %s FOR UPDATE NOWAIT',
                (self.id,),
                log_exceptions=False
            )
        except LockNotAvailable:
            _logger.warning(
                "User %s tried to process bulk CVs for job %s, "
                "but it was locked.",
                self.env.user.id, self.id
            )
            raise UserError(_(
                "This job is currently being processed by another user or "
                "background task. Please try again later."
            ))

        if self.bulk_processing_in_progress:
            raise UserError(_(
                "Processing is already in progress. "
                "Please wait until it is complete."
            ))

        if not self.cv_attachment_ids:
            raise UserError(_("Please attach CV files before processing."))

        attachments_to_process = (
            self.cv_attachment_ids - self.processed_cv_attachment_ids
        )

        if not attachments_to_process:
            raise UserError(_(
                "All attached CVs have already been processed successfully. "
                "Please delete the attached files to start a new batch."
            ))

        _logger.info(
            "--- Button 'action_process_cvs' TRIGGERED by user %s "
            "for %s CVs ---",
            self.env.user.name, len(attachments_to_process)
        )

        job_record = self.with_delay()._process_cvs_thread(
            self.env.user.id, attachments_to_process.ids
        )

        self.write({
            'processed_cv_attachment_ids': [(5, 0, 0)],
            'processed_cv_count': 0,
            'failed_cv_count': 0,
            'total_cv_count': len(attachments_to_process),
            'bulk_processing_complete': False,
            'bulk_processing_failed': False,
            'bulk_queue_job_uuid': job_record.uuid,
        })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Processing Started'),
                'message': _(
                    'Processing has started for %s CV(s). '
                    'You will be notified upon completion.',
                    len(attachments_to_process)
                ),
                'type': 'info',
                'sticky': False,
            }
        }

    def action_delete_cv_attachments(self):
        """
        Triggered by the 'Delete Attached CVs' button.
        """
        self.ensure_one()
        attachment_count = len(self.cv_attachment_ids)

        to_unlink = (
            self.cv_attachment_ids | self.processed_cv_attachment_ids
        )
        if to_unlink:
            to_unlink.unlink()

        self.write({
            'cv_attachment_ids': [(5, 0, 0)],
            'processed_cv_attachment_ids': [(5, 0, 0)],
            'bulk_processing_complete': False,
            'bulk_processing_failed': False,
            'bulk_queue_job_uuid': False,
            'processed_cv_count': 0,
            'failed_cv_count': 0,
            'total_cv_count': 0,
        })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Files Deleted'),
                'message': _(
                    '%s attached CVs for this job have been deleted.',
                    attachment_count
                ),
                'type': 'success',
                'sticky': False,
            }
        }

    # --- Actions for JD Parsing ---

    def action_generate_requirements_from_file(self):
        """
        Button action to parse the 'job_description_attachment_ids'
        and generate 'requirement_statement_ids' via AI.
        """
        self.ensure_one()

        if not self.job_description_attachment_ids:
            raise UserError(_("Please upload a Job Description file first."))

        if len(self.job_description_attachment_ids) > 1:
            raise UserError(_(
                "Please upload only ONE Job Description file. "
                "You have uploaded %s."
            ) % len(self.job_description_attachment_ids))

        attachment = self.job_description_attachment_ids[0]

        if self.jd_processing_in_progress:
            raise UserError(_("Processing is already in progress. Please wait."))

        if (attachment in self.jd_processed_attachment_ids and
                self.jd_extract_state != 'error'):
            # Allow re-processing by just clearing the old statements
            _logger.info(
                "Re-processing JD file for job %s. "
                "Deleting %s old requirements.",
                self.id, len(self.requirement_statement_ids)
            )
            self.requirement_statement_ids.unlink()

        # Clear processed attachments to mark this as a new run
        self.write({
            'jd_extract_state': 'pending',
            'jd_extract_status': _(
                'Pending: Queued for requirement extraction...'
            ),
            'jd_processed_attachment_ids': [(5, 0, 0)],
        })

        user_id = self.env.user.id
        job_record = self.with_delay()._run_jd_extraction_job(
            user_id, attachment.id
        )

        self.jd_queue_job_uuid = job_record.uuid

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Processing Started'),
                'message': _(
                    'The Job Description is being analyzed by AI. '
                    'You will be notified upon completion.'
                ),
                'type': 'info',
                'sticky': False,
            }
        }

    def action_delete_jd_attachment(self):
        """
        Triggered by the 'Delete Attached JD' button.
        Removes the attachment and resets all flags, BUT keeps requirements.
        """
        self.ensure_one()
        attachments = (
            self.job_description_attachment_ids |
            self.jd_processed_attachment_ids
        )

        self.write({
            'job_description_attachment_ids': [(5, 0, 0)],
            'jd_processed_attachment_ids': [(5, 0, 0)],
            'jd_extract_state': 'no_extract',
            'jd_extract_status': False,
            'jd_queue_job_uuid': False,
        })

        if attachments:
            attachments.unlink()

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('File Deleted'),
                'message': _(
                    'The Job Description file has been deleted. '
                    'Existing requirements were preserved.'
                ),
                'type': 'success',
                'sticky': False,
            }
        }

    # --- Bulk Processing Helpers ---

    def _process_bulk_extraction(self, attachments):
        """
        Phase 1: Extract CVs and create Applicants.
        Returns tuple: (successful_ids, errors, fail_count)
        """
        successful_ids = []
        errors = []
        fail_count = 0

        for att in attachments:
            extracted_data = None
            applicant_id = None
            error_msg = None
            cv_name_for_log = att.name

            # 1.1 Extraction Transaction
            try:
                with odoo.registry(self.env.cr.dbname).cursor() as work_cr:
                    work_env = api.Environment(
                        work_cr, self.env.uid, self.env.context
                    )

                    # Securely read binary data into memory
                    att_record = work_env['ir.attachment'].browse(att.id)
                    cv_name = att_record.name
                    cv_datas = att_record.datas

                    if not cv_datas:
                        raise UserError(_(
                            "Skipping CV %s: Attachment data is empty.",
                            cv_name
                        ))

                    _logger.info(f"Bulk Extraction: Processing {cv_name}")

                    # Call AI
                    ApplicantEnv = work_env['hr.applicant']
                    response_model = ApplicantEnv._openai_call(
                        att_record,
                        prompt=OPENAI_CV_EXTRACTION_PROMPT,
                        text_format=CVExtraction
                    )
                    extracted_data = response_model.model_dump(mode='json')

                    # Create Applicant
                    name_part = (
                        extracted_data.get('name') or
                        cv_name.rsplit('.', 1)[0]
                    )
                    new_applicant = ApplicantEnv.create({
                        'name': _("%s's Application") % name_part,
                        'partner_name': extracted_data.get('name'),
                        'email_from': extracted_data.get('email'),
                        'partner_phone': extracted_data.get('phone'),
                        'job_id': self.id,
                        'openai_extract_state': 'done',
                        'openai_extract_status': _('Created from bulk import.'),
                    })

                    # Process Details
                    status_msg = new_applicant._process_extracted_cv_data(
                        extracted_data
                    )
                    new_applicant.write({'openai_extract_status': status_msg})

                    # Create/Link Attachment
                    new_att = work_env['ir.attachment'].create({
                        'name': cv_name,
                        'datas': cv_datas,
                        'res_model': 'hr.applicant',
                        'res_id': new_applicant.id
                    })
                    new_applicant.write({
                        'message_main_attachment_id': new_att.id
                    })

                    applicant_id = new_applicant.id
                    work_cr.commit()

            except Exception as e:
                error_msg = str(e)
                _logger.error(
                    f"Extraction failed for {cv_name_for_log}: {error_msg}",
                    exc_info=True
                )

            # 1.2 Stats Update Transaction (Brief Lock)
            try:
                with odoo.registry(self.env.cr.dbname).cursor() as stats_cr:
                    stats_env = api.Environment(
                        stats_cr, self.env.uid, self.env.context
                    )
                    stats_env.cr.execute(
                        'SELECT * FROM hr_job WHERE id = %s FOR UPDATE',
                        (self.id,),
                        log_exceptions=False
                    )
                    stats_job = stats_env['hr.job'].browse(self.id)

                    if error_msg:
                        stats_job.failed_cv_count += 1
                        errors.append(f"{cv_name_for_log}: {error_msg}")
                        fail_count += 1
                    else:
                        stats_job.processed_cv_count += 1
                        stats_job.processed_cv_attachment_ids = [(4, att.id)]
                        successful_ids.append(applicant_id)

                    stats_cr.commit()
            except Exception as e_stats:
                _logger.critical(
                    f"Stats update failed for {cv_name_for_log}: {e_stats}",
                    exc_info=True
                )

        return successful_ids, errors, fail_count

    def _process_bulk_matching(self, user_id, successful_applicant_ids):
        """
        Phase 5: Run AI Match for successful applicants.
        Returns tuple: (match_success_count, match_fail_count)
        """
        match_success_count = 0
        match_fail_count = 0

        for app_id in successful_applicant_ids:
            try:
                with odoo.registry(self.env.cr.dbname).cursor() as match_cr:
                    match_env = api.Environment(
                        match_cr, self.env.uid, self.env.context
                    )
                    app_record = match_env['hr.applicant'].browse(app_id)

                    # Set status
                    app_record.write({
                        'ai_match_state': 'processing',
                        'ai_match_status': _('Processing: Running AI Match...')
                    })
                    match_cr.commit()

                    # Run logic
                    try:
                        app_record._run_ai_match_job(user_id)
                        match_success_count += 1
                    except Exception as e_inner:
                        app_record.write({
                            'ai_match_state': 'error',
                            'ai_match_status': f"Match Failed: {str(e_inner)}"
                        })
                        match_fail_count += 1

                    match_cr.commit()
            except Exception as e_match_tx:
                _logger.error(
                    f"Match Tx failed for applicant {app_id}: {e_match_tx}",
                    exc_info=True
                )
                match_fail_count += 1

        return match_success_count, match_fail_count

    # --- Background Logic for Bulk CV Upload ---

    def _process_cvs_thread(self, user_id, attachment_ids_to_process):
        """
        Background job for bulk processing.
        """
        self.ensure_one()

        attachments = self.env['ir.attachment'].browse(
            attachment_ids_to_process
        )

        # --- PHASE 1: BULK EXTRACTION ---
        # Unpack the results of the extraction phase
        (
            successful_applicant_ids,
            extraction_errors,
            total_extract_fail
        ) = self._process_bulk_extraction(attachments)
        total_extract_success = len(successful_applicant_ids)

        # --- PHASE 2: NOTIFY BULK COMPLETION ---
        try:
            with odoo.registry(self.env.cr.dbname).cursor() as notify_cr:
                notify_env = api.Environment(
                    notify_cr, self.env.uid, self.env.context
                )

                msg = _(
                    "Bulk CV Extraction Finished.\n%s Succeeded, %s Failed.",
                    total_extract_success, total_extract_fail
                )
                if extraction_errors:
                    msg += _("\nErrors:\n- ") + "\n- ".join(extraction_errors)

                params = {
                    'title': _('Extraction Complete'),
                    'message': msg,
                    'type': 'success' if total_extract_fail == 0 else 'warning',
                    'sticky': False,
                }
                notify_env['hr.applicant']._notify_user(user_id, params)
                notify_cr.commit()
        except Exception as e:
            _logger.error("Failed to send extraction notification: %s", e)

        # --- PHASE 3 & 4: CHECK & NOTIFY AI MATCH START ---
        should_run_match = (
            self.run_ai_match_on_bulk and
            self.requirement_statement_ids and
            successful_applicant_ids
        )

        if should_run_match:
            try:
                with odoo.registry(self.env.cr.dbname).cursor() as notify_cr:
                    notify_env = api.Environment(
                        notify_cr, self.env.uid, self.env.context
                    )
                    params = {
                        'title': _('AI Match Started'),
                        'message': _(
                            'Starting AI Match analysis for %s candidates...',
                            len(successful_applicant_ids)
                        ),
                        'type': 'info',
                        'sticky': False,
                    }
                    notify_env['hr.applicant']._notify_user(user_id, params)
                    notify_cr.commit()
            except Exception as e:
                _logger.error("Failed to send match start notification: %s", e)

            # --- PHASE 5: RUN AI MATCH BULK ---
            match_success_count, match_fail_count = self._process_bulk_matching(
                user_id, successful_applicant_ids
            )

            # --- PHASE 6: NOTIFY AI MATCH FINISH ---
            try:
                with odoo.registry(self.env.cr.dbname).cursor() as notify_cr:
                    notify_env = api.Environment(
                        notify_cr, self.env.uid, self.env.context
                    )
                    params = {
                        'title': _('AI Match Bulk Finished'),
                        'message': _(
                            'AI Match complete.\n%s Succeeded, %s Failed.',
                            match_success_count, match_fail_count
                        ),
                        'type': 'success',
                        'sticky': False,
                    }
                    notify_env['hr.applicant']._notify_user(user_id, params)
                    notify_cr.commit()
            except Exception as e:
                _logger.error("Failed to send match finish notification: %s", e)

        # --- Finalize Job State ---
        try:
            with odoo.registry(self.env.cr.dbname).cursor() as final_cr:
                final_env = api.Environment(
                    final_cr, self.env.uid, self.env.context
                )
                try:
                    final_env.cr.execute(
                        'SELECT * FROM hr_job WHERE id = %s FOR UPDATE',
                        (self.id,),
                        log_exceptions=False
                    )
                except Exception:
                    return

                final_job = final_env['hr.job'].browse(self.id)
                final_job.write({
                    'bulk_processing_complete': True,
                    'bulk_processing_failed': total_extract_fail > 0,
                    'bulk_queue_job_uuid': False,
                })
                final_cr.commit()
        except Exception as e_fin:
            _logger.error(f"Finalize failed: {e_fin}")

    # --- Background Logic for JD Parsing ---

    def _run_jd_extraction_job(self, user_id, attachment_id):
        """
        Job queue method to run the JD extraction.
        """
        self.ensure_one()
        job = self
        attachment = self.env['ir.attachment'].browse(attachment_id)
        if not attachment.exists():
            _logger.warning(
                "Attachment %s deleted before job could run. Aborting.",
                attachment_id
            )
            job.write({
                'jd_extract_state': 'error',
                'jd_extract_status': _(
                    "File was deleted before processing could start."
                ),
                'jd_queue_job_uuid': False,
            })
            return

        success = False
        error_message = ""

        try:
            with self.env.cr.savepoint():
                job.write({
                    'jd_extract_state': 'processing',
                    'jd_extract_status': _('Processing: Calling AI service...'),
                })

                requirements_data = self._execute_jd_extract_single(attachment)

            with self.env.cr.savepoint():
                self._process_jd_extract_data(requirements_data)

            job.write({
                'jd_extract_state': 'done',
                'jd_extract_status': _(
                    'Successfully generated %s requirements.',
                    len(requirements_data)
                ),
                'jd_processed_attachment_ids': [(6, 0, [attachment.id])],
            })
            success = True

        except Exception as e:
            _logger.error(
                "Job Description extraction for job %s failed: %s",
                job.id, str(e), exc_info=True
            )
            self.env.cr.rollback()
            error_message = _("Error: %s", str(e))
            job.write({
                'jd_extract_state': 'error',
                'jd_extract_status': error_message,
            })
            self.env.cr.commit()
            success = False

        finally:
            job.write({'jd_queue_job_uuid': False})

            params = {}
            if success:
                params = {
                    'title': _('Requirements Generated'),
                    'message': _(
                        "Successfully generated requirements for job '%s'.",
                        job.name
                    ),
                    'type': 'success',
                    'sticky': False,
                }
            elif error_message:
                params = {
                    'title': _('Requirement Generation Failed'),
                    'message': _(
                        "Failed to generate requirements for '%s'.\n%s",
                        job.name, error_message
                    ),
                    'type': 'warning',
                    'sticky': True,
                }
            if params:
                self.env['hr.applicant']._notify_user(user_id, params)

    def _execute_jd_extract_single(self, attachment):
        """
        Runs the 'single_prompt' extraction logic using Pydantic.
        """
        self.ensure_one()
        ApplicantEnv = self.env['hr.applicant']

        try:
            response_model = ApplicantEnv._openai_call(
                attachment,
                prompt=JD_EXTRACT_SINGLE_PROMPT,
                text_format=JDRequirementList
            )

            if not response_model or not response_model.requirements:
                _logger.warning(
                    "JD extraction returned no requirements for job %s.",
                    self.id
                )
                return []

            return [req.model_dump() for req in response_model.requirements]

        except Exception as e:
            _logger.error(
                "Failed during Pydantic-based JD extraction: %s",
                str(e),
                exc_info=True
            )
            raise

    def _process_jd_extract_data(self, requirements_data_list):
        """
        Takes the list of requirement dicts from the AI and creates records.
        """
        self.ensure_one()
        ReqTag = self.env['hr.job.requirement.tag']
        Req = self.env['hr.job.requirement']

        if not requirements_data_list:
            raise UserError(_("AI processing returned no requirements."))

        self.requirement_statement_ids.unlink()

        tag_cache = {}
        tag_names = list(set(
            r['tag_name'] for r in requirements_data_list if r.get('tag_name')
        ))

        if tag_names:
            existing_tags = ReqTag.search([('name', 'in', tag_names)])
            tag_cache = {tag.name.lower(): tag.id for tag in existing_tags}

            new_tags_to_create = []
            for tag_name in tag_names:
                if tag_name.lower() not in tag_cache:
                    new_tags_to_create.append({'name': tag_name})

            if new_tags_to_create:
                try:
                    new_tags = ReqTag.create(new_tags_to_create)
                    for tag in new_tags:
                        tag_cache[tag.name.lower()] = tag.id
                except Exception as e:
                    _logger.error(
                        "Failed to create new requirement tags: %s", str(e)
                    )
                    pass

        new_req_vals_list = []
        for seq, req_data in enumerate(requirements_data_list):
            tag_name = req_data.get('tag_name')
            tag_id = tag_cache.get(tag_name.lower()) if tag_name else False

            new_req_vals_list.append({
                'name': req_data['name'],
                'job_id': self.id,
                'weight': req_data.get('weight', 1.0),
                'tag_ids': [(6, 0, [tag_id])] if tag_id else False,
                'sequence': (seq + 1) * 10
            })

        if new_req_vals_list:
            _logger.info(
                "Creating %s new requirements for job %s",
                len(new_req_vals_list), self.id
            )
            Req.create(new_req_vals_list)