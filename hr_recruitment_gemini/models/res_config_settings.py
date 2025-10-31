# -*- coding: utf-8 -*-
from odoo import fields, models

class ResConfigSettings(models.TransientModel):
    """
    Inherits `res.config.settings` to expose the Gemini-related
    fields from `res.company` in the main settings view, allowing
    users to configure the feature easily.
    """
    _inherit = 'res.config.settings'

    gemini_cv_extract_mode = fields.Selection(
        related='company_id.gemini_cv_extract_mode',
        string='CV Data Extraction (Gemini)',
        readonly=False,
        required=True)

    gemini_api_key = fields.Char(
        related='company_id.gemini_api_key',
        string="Gemini API Key",
        readonly=False)

    gemini_model = fields.Char(
        related='company_id.gemini_model',
        string="Gemini Model",
        readonly=False
    )
