# -*- coding: utf-8 -*-
{
    'name': 'HR Recruitment Gemini Extract',
    'version': '17.0.1.0.0',
    'category': 'Human Resources/Recruitment',
    'summary': "Extract data from CVs using Gemini to fill applicant forms.",
    'description': """
This module replaces the Odoo Enterprise 'iap_extract' functionality for HR applicants.
It adds a button to scan CV attachments (PDFs) and uses the Google Gemini API
to extract and populate the applicant's name, email, phone, skills, and degree.
    """,
    'author': 'alextranduil',
    'website': '',
    'depends': [
        'hr_recruitment', 
        'mail',
        'hr_recruitment_skills',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/hr_applicant_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'assets': {},
    'external_dependencies': {
        'python': [
            'google.generativeai',
        ],
    },
    'license': 'LGPL-3',
    'installable': True,
    'application': False,
    'auto_install': False,
}
