# -*- coding: utf-8 -*-
from odoo import fields, models, api

class HrApplicantMatchStatement(models.Model):
    """
    Stores the AI's evaluation of a single job requirement
    for a specific applicant.
    """
    _name = 'hr.applicant.match.statement'
    _description = 'Applicant Match Statement'
    _order = 'requirement_id'

    applicant_id = fields.Many2one(
        'hr.applicant',
        string='Applicant',
        required=True,
        ondelete='cascade'
    )
    job_id = fields.Many2one(
        related='applicant_id.job_id',
        store=True
    )
    requirement_id = fields.Many2one(
        'hr.job.requirement',
        string='Requirement',
        required=True,
        ondelete='cascade'
    )
    requirement_name = fields.Char(
        related='requirement_id.name',
        string='Requirement'
    )
    requirement_weight = fields.Float(
        related='requirement_id.weight',
        string='Weight',
        store=True
    )

    match_fit = fields.Selection(
        selection=[
            ('not_fit', 'Not a Fit'),
            ('poor_fit', 'Poor Fit'),
            ('fit', 'Fit'),
            ('good_fit', 'Good Fit'),
            ('excellent_fit', 'Excellent Fit'),
        ],
        string='Fit',
        default='not_fit',
        required=True,
    )

    match_score = fields.Float(
        string='Score (%)',
        compute='_compute_match_score',
        store=True,
        digits=(16, 2),
        help="Numeric score from 0 to 100 based on the 'Fit' level."
    )

    explanation = fields.Text(
        string='Explanation',
        help="AI's explanation for the score, citing resume text."
    )

    @api.depends('match_fit')
    def _compute_match_score(self):
        """Converts the selection 'fit' field to a numeric score."""
        score_map_default = {
            'not_fit': 0.0,
            'poor_fit': 25.0,
            'fit': 50.0,
            'good_fit': 80.0,
            'excellent_fit': 100.0,
        }
        for record in self:
            record.match_score = score_map_default.get(record.match_fit, 0.0)