# -*- coding: utf-8 -*-
from odoo import fields, models

class ResCompany(models.Model):
    """
    Inherits `res.company` to store company-wide settings
    for the Gemini CV extraction feature.
    """
    _inherit = 'res.company'

    gemini_cv_extract_mode = fields.Selection(
        selection=[
            ('no_send', 'Do not extract'),
            ('manual_send', "Extract on demand only"),
        ],
        string="CV Data Extraction (Gemini)",
        required=True,
        default='manual_send',
        help="""Determines how CVs are processed:
                - Do not extract: Disables the feature.
                - Extract on demand only: Enables the 'Extract with Gemini' button for manual triggering."""
    )

    gemini_api_key = fields.Char(
        string="Gemini API Key",
        copy=False,
        help="Your secret API Key for Google's Gemini service.")

    gemini_model = fields.Char(
        string="Gemini Model",
        copy=False,
        default="gemini-2.5-flash-lite",
        help="Specify the Gemini model to use (e.g., 'gemini-2.5-flash-latest', 'gemini-2.5-flash-lite')."
    )
