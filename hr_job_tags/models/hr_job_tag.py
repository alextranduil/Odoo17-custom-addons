from odoo import models, fields

class HrJobTag(models.Model):
    _name = 'hr.job.tag'
    _description = 'Job Position Tags'
    _order = 'name'

    name = fields.Char(string='Tag Name', required=True, translate=True)
    color = fields.Integer(string='Color Index')
    job_ids = fields.Many2many('hr.job', string='Job Positions')

    _sql_constraints = [
        ('name_uniq', 'unique (name)', 'Tag name already exists!')
    ]