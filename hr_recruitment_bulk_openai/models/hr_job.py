# -*- coding: utf-8 -*-
import base64
import json
import logging
import odoo
import re # Import re, as _parse_openai_response uses it

from odoo import api, fields, models, _
from odoo.exceptions import UserError
# Import the prompt from your base module
from odoo.addons.hr_recruitment_openai.models.hr_applicant import OPENAI_CV_EXTRACTION_PROMPT

_logger = logging.getLogger(__name__)


class HrJob(models.Model):
    _inherit = 'hr.job'

    cv_attachment_ids = fields.Many2many(
        'ir.attachment',
        'hr_job_cv_attachment_rel',
        'job_id',
        'attachment_id',
        string='CVs to Process',
        help="Upload multiple CVs here to create applicants in bulk."
    )
    processing_in_progress = fields.Boolean(
        string="Processing CVs",
        default=False,
        copy=False,
        help="Indicates that CVs are currently being processed in the background."
    )
    processing_complete = fields.Boolean(
        string="Processing Complete",
        default=False,
        copy=False,
        help="Indicates that a bulk processing has been completed."
    )

    # --- Button Actions ---

    def action_process_cvs(self):
        """
        Triggered by the 'Add Candidates' button.
        Sets flags and launches the background job.
        """
        self.ensure_one()

        try:
            # --- Database lock to prevent double-clicks ---
            self.env.cr.execute('SELECT * FROM hr_job WHERE id = %s FOR UPDATE', (self.id,))
            
            # Re-browse to get the freshest data
            job = self.browse(self.id)
            # --- END LOCK ---

            # --- Perform checks on the FRESH record ---
            if job.processing_in_progress:
                raise UserError(_("Processing is already in progress. Please wait until it is complete."))
            
            if job.processing_complete:
                raise UserError(_("Processing has already been completed for these files. Please delete the attached files to start a new batch."))

            if not job.cv_attachment_ids:
                raise UserError(_("Please attach CV files before processing."))

            _logger.info("--- Button 'action_process_cvs' TRIGGERED by user %s ---", self.env.user.name)

            # Write the flags. This is now safe.
            job.write({
                'processing_in_progress': True,
                'processing_complete': False
            })

            # Pass the user ID to notify the correct user
            job.with_delay()._process_cvs_thread(self.env.user.id)

            # Return a toast notification to the user
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Processing Started'),
                    'message': _('The CV processing has started. You will be notified upon completion.'),
                    'type': 'info',
                    'sticky': False,
                }
            }
        
        except Exception:
            # The transaction will roll back, releasing the lock.
            raise


    def action_delete_cv_attachments(self):
        """
        Triggered by the 'Delete Attached Files' button.
        Removes the attachments from the job and resets flags.
        
        --- REVERTED to simple version ---
        This will leave orphan files in the chatter, but it
        will avoid the AttributeError.
        """
        self.ensure_one()
        attachment_count = len(self.cv_attachment_ids)
        
        # Unlink the attachments themselves
        if self.cv_attachment_ids:
            self.cv_attachment_ids.unlink()
        
        # Clear the m2m relation and reset flags
        self.write({
            'cv_attachment_ids': [(5, 0, 0)],
            'processing_complete': False,
            'processing_in_progress': False, # Just in case
        })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Files Deleted'),
                'message': _('%s attached CVs for this job have been deleted.', attachment_count),
                'type': 'success',
                'sticky': False,
            }
        }

    # --- Background Processing ---

    def _process_cvs_thread(self, user_id):
        """
        This method runs in the background via the Odoo job queue.
        It processes all attached CVs, creates applicants, and notifies the user.
        """
        self.ensure_one()
        
        user = self.env['res.users'].browse(user_id)
        partner = user.partner_id

        ApplicantEnv = self.env['hr.applicant']
        AttachmentEnv = self.env['ir.attachment']
        
        success_count = 0
        fail_count = 0
        errors = []
        
        attachments = self.cv_attachment_ids

        try:
            for att in attachments:
                try:
                    with self.env.cr.savepoint():
                        _logger.info(f"Processing CV: {att.name} for job {self.name}")
                        
                        if not att.datas:
                            _logger.warning(f"Skipping CV {att.name}: Attachment data is empty.")
                            continue

                        # 1. Call OpenAI
                        response_text = ApplicantEnv._openai_call_for_cv(att)
                        
                        # 2. Parse response
                        log_id = f"job_{self.id}_att_{att.id}"
                        data_dict = ApplicantEnv._parse_openai_response(response_text, record_id=log_id)

                        # 3. Create new applicant
                        create_vals = {
                            'name': data_dict.get('name') or _("%s's Application") % att.name.rsplit('.', 1)[0],
                            'partner_name': data_dict.get('name'),
                            'email_from': data_dict.get('email'),
                            'partner_phone': data_dict.get('phone'),
                            'job_id': self.id,
                            'openai_extract_state': 'done',
                            'openai_extract_status': _('Created from bulk import. Processing data...'),
                        }
                        
                        new_applicant = ApplicantEnv.create(create_vals)
                        _logger.info(f"Created new applicant: {new_applicant.name} (ID: {new_applicant.id})")

                        # 4. Call processing method ON THE NEW APPLICANT
                        status_msg = new_applicant._process_extracted_cv_data(data_dict)
                        new_applicant.write({'openai_extract_status': status_msg})

                        # 5. Attach original CV to the new applicant
                        AttachmentEnv.create({
                            'name': att.name,
                            'datas': att.datas,
                            'res_model': 'hr.applicant',
                            'res_id': new_applicant.id,
                        })
                        
                        success_count += 1
                        _logger.info(f"Successfully processed applicant: {new_applicant.name}")

                except Exception as e:
                    # This catches errors for *one* CV
                    _logger.error(f"Failed to process CV {att.name} for job {self.name}: {e}")
                    fail_count += 1
                    errors.append(f"{att.name}: {str(e)}")
                
                # self.env.cr.commit()  <--- THIS IS REMOVED (The fix)

        except Exception as e:
            # This catches a critical job-level error
            _logger.error(f"Critical error during CV processing job {self.name}: {e}", exc_info=True)
            self.env.cr.rollback() 
            errors.append(f"Critical Job Failure: {str(e)}")

        finally:
            # All files processed, update job state
            self.browse(self.id).write({
                'processing_in_progress': False,
                'processing_complete': True
            })

            # 6. Send final notification
            message = _("CV processing finished for job '%s'.\n%s applicants created.\n%s failed.", 
                        self.name, success_count, fail_count)
            if errors:
                message += _("\nErrors:\n- ") + "\n- ".join(errors)

            params = {
                'title': _('Processing Complete'),
                'message': message,
                'type': 'success' if fail_count == 0 else 'warning',
                'sticky': True,
            }
            self.env['bus.bus']._sendone(partner, 'simple_notification', params)