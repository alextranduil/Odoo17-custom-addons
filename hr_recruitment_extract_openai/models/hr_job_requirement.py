# -*- coding: utf-8 -*-
from odoo import fields, models


class HrJobRequirement(models.Model):
    """
    Represents a single requirement (statement) for a job position.
    These are generated from the Job Description file.
    """
    _name = 'hr.job.requirement'
    _description = 'Job Requirement Statement'
    _order = 'sequence, id'

    name = fields.Char(string='Requirement', required=True)
    sequence = fields.Integer(default=10)

    job_id = fields.Many2one(
        'hr.job',
        string='Job Position',
        required=True,
        ondelete='cascade'
    )

    weight = fields.Float(
        string='Weight',
        default=1.0,
        digits=(16, 2),
        help="Importance of this requirement. "
             "Higher weight = more impact on the final score."
    )

    tag_ids = fields.Many2many(
        'hr.job.requirement.tag',
        string='Tags',
        help="Classify this requirement (e.g., Hard Skill, Soft Skill)."
    )

    company_relevance_ids = fields.Many2many(
        'res.partner',
        string='Relevant Companies',
        help="Specify companies where experience in this area is a "
             "significant plus."
    )

    _sql_constraints = [
        ('weight_positive', 'CHECK(weight > 0)', 'Weight must be positive.'),
    ]