# -*- coding: utf-8 -*-
from odoo import fields, models

class ResCompany(models.Model):
    """
    Inherits `res.company` to store company-wide settings
    for the OpenAI CV extraction feature.
    """
    _inherit = 'res.company'

    openai_cv_extract_mode = fields.Selection(
        selection=[
            ('no_send', 'Do not extract'),
            ('manual_send', "Extract on demand only"),
        ],
        string="CV Data Extraction (OpenAI)",
        required=True,
        default='manual_send',
        help="""Determines how CVs are processed:
                - Do not extract: Disables the feature.
                - Extract on demand only: Enables the 'Extract with OpenAI' button for manual triggering."""
    )

    openai_api_key = fields.Char(
        string="OpenAI API Key",
        copy=False,
        help="Your secret API Key for OpenAI service.")

    openai_model = fields.Char(
        string="OpenAI Model",
        copy=False,
        default="gpt-4o-mini",
        help="Specify the OpenAI model to use (e.g., 'gpt-4o-mini', 'gpt-4o')."
    )
