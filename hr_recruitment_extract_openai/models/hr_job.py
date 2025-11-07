# -*- coding: utf-8 -*-
import base64
import json
import logging
import odoo
import re

from odoo import api, fields, models, _
from odoo.exceptions import UserError

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
    processed_cv_attachment_ids = fields.Many2many(
        'ir.attachment',
        'hr_job_cv_attachment_processed_rel',
        'job_id',
        'attachment_id',
        string='Processed CVs',
        copy=False,
        readonly=True,
        help="CVs that have been successfully processed and had an applicant created."
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
    processing_failed = fields.Boolean(
        string="Processing Failed",
        default=False,
        copy=False,
        help="Indicates that one or more CVs failed during the last processing run."
    )

    # --- Button Actions ---

    def action_process_cvs(self):
        """
        Triggered by the 'Add Candidates' button.
        Calculates only the CVs that have not been processed yet and
        launches a background job for them.
        """
        self.ensure_one()

        try:
            # Database lock to prevent double-clicks
            self.env.cr.execute('SELECT * FROM hr_job WHERE id = %s FOR UPDATE', (self.id,))
            
            # Re-browse to get the freshest data
            job = self.browse(self.id)

            # Perform checks on the FRESH record
            if job.processing_in_progress:
                raise UserError(_("Processing is already in progress. Please wait until it is complete."))
            
            if not job.cv_attachment_ids:
                raise UserError(_("Please attach CV files before processing."))

            # Calculate which attachments to process
            attachments_to_process = job.cv_attachment_ids - job.processed_cv_attachment_ids

            if not attachments_to_process:
                raise UserError(_("All attached CVs have already been processed successfully. Please delete the attached files to start a new batch."))

            _logger.info(
                "--- Button 'action_process_cvs' TRIGGERED by user %s for %s CVs ---",
                self.env.user.name, len(attachments_to_process)
            )

            # Write the flags. This is now safe.
            job.write({
                'processing_in_progress': True,
                'processing_complete': False,
                'processing_failed': False
            })

            # Pass the user ID and attachment IDs to the job
            job.with_delay()._process_cvs_thread(self.env.user.id, attachments_to_process.ids)

            # Return a toast notification to the user
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Processing Started'),
                    'message': _(
                        'Processing has started for %s CV(s). You will be notified upon completion.',
                        len(attachments_to_process)
                    ),
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
        Removes the attachments from the job and resets all flags.
        """
        self.ensure_one()
        attachment_count = len(self.cv_attachment_ids)
        
        # Unlink the attachments themselves
        if self.cv_attachment_ids:
            self.cv_attachment_ids.unlink()
        
        # Clear the m2m relation and reset flags
        self.write({
            'cv_attachment_ids': [(5, 0, 0)],
            'processed_cv_attachment_ids': [(5, 0, 0)],
            'processing_complete': False,
            'processing_in_progress': False,
            'processing_failed': False,
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

    def _notify_user(self, user_id, params):
        """Helper to send a notification to a specific user."""
        try:
            # Use a new cursor to ensure notification is sent
            with odoo.registry(self.env.cr.dbname).cursor() as notify_cr:
                notify_env = api.Environment(notify_cr, self.env.uid, self.env.context)
                user = notify_env['res.users'].browse(user_id)
                if user.partner_id:
                    notify_env['bus.bus']._sendone(user.partner_id, 'simple_notification', params)
                notify_cr.commit()
        except Exception as e:
            _logger.error("Failed to send notification to user %s: %s", user_id, str(e))

    # --- Background Processing ---

    def _process_cvs_thread(self, user_id, attachment_ids_to_process):
        """
        This method runs in the background via the Odoo job queue.
        It processes only the specified CVs, creates applicants, and notifies the user.
        """
        self.ensure_one()
        
        ApplicantEnv = self.env['hr.applicant']
        AttachmentEnv = self.env['ir.attachment']
        
        success_count = 0
        fail_count = 0
        errors = []
        
        attachments = AttachmentEnv.browse(attachment_ids_to_process)
        critical_error = False

        try:
            for att in attachments:
                try:
                    # Use a savepoint for each attachment to isolate failures
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

                        # Standardize Applicant Name
                        applicant_name_str = data_dict.get('name') or att.name.rsplit('.', 1)[0]
                        
                        # 3. Create new applicant
                        create_vals = {
                            'name': _("%s's Application") % applicant_name_str,
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
                        # Mark this CV as processed
                        self.write({'processed_cv_attachment_ids': [(4, att.id)]})
                        _logger.info(f"Successfully processed applicant: {new_applicant.name}")

                except Exception as e:
                    # This catches errors for *one* CV
                    _logger.error(f"Failed to process CV {att.name} for job {self.name}: {e}", exc_info=True)
                    fail_count += 1
                    errors.append(f"{att.name}: {str(e)}")
                    # The savepoint automatically rolls back this CV's transaction

        except Exception as e:
            # This catches a critical, job-stopping error (e.g., in setup)
            critical_error = True
            _logger.error(f"Critical error during CV processing job {self.name}: {e}", exc_info=True)
            self.env.cr.rollback() 
            errors.append(f"Critical Job Failure: {str(e)}")

        finally:
            # This block *always* runs and *always* sends a notification.
            try:
                # Update job state
                final_vals = {
                    'processing_in_progress': False,
                    'processing_complete': True,
                    'processing_failed': bool(fail_count > 0 or critical_error) 
                }
                self.browse(self.id).write(final_vals)

            except Exception as e_finally:
                # If the *final write* fails, we have a critical problem.
                critical_error = True
                _logger.error(f"Critical error during finally block for job {self.name}: {e_finally}", exc_info=True)
                errors.append(f"Critical Finally Block Error: {str(e_finally)}")

            # Send final notification
            job_failed = bool(fail_count > 0 or critical_error)
            message = _("CV processing finished for job '%s'.\n%s applicants created.\n%s failed.", 
                        self.name, success_count, fail_count)
            if errors:
                message += _("\nErrors:\n- ") + "\n- ".join(errors)

            params = {
                'title': _('Processing Complete') if not job_failed else _('Processing Finished with Errors'),
                'message': message,
                'type': 'success' if not job_failed else 'warning',
                'sticky': job_failed, # Make notification sticky if there was an error
            }
            self._notify_user(user_id, params)