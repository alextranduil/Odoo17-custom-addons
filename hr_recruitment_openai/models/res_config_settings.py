# -*- coding: utf-8 -*-
from odoo import fields, models

class ResConfigSettings(models.TransientModel):
    """
    Inherits `res.config.settings` to expose the OpenAI-related
    fields from `res.company` in the main settings view, allowing
    users to configure the feature easily.
    """
    _inherit = 'res.config.settings'

    openai_cv_extract_mode = fields.Selection(
        related='company_id.openai_cv_extract_mode',
        string='CV Data Extraction (OpenAI)',
        readonly=False,
        required=True)

    openai_api_key = fields.Char(
        related='company_id.openai_api_key',
        string="OpenAI API Key",
        readonly=False)

    openai_model = fields.Char(
        related='company_id.openai_model',
        string="OpenAI Model",
        readonly=False
    )
