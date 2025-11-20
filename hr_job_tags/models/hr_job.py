from odoo import models, fields

class HrJob(models.Model):
    _inherit = 'hr.job'

    tag_ids = fields.Many2many(
        'hr.job.tag', 
        string='Tags',
        help="Classify and filter your job positions with tags."
    )