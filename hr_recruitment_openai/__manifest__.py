# -*- coding: utf-8 -*-
{
    'name': 'HR Recruitment OpenAI Extract',
    'version': '17.0.1.0.0',
    'category': 'Human Resources/Recruitment',
    'summary': "Extract data from CVs using OpenAI to fill applicant forms.",
    'description': """
This module provides functionality to scan CV attachments (PDFs, etc.)
and use the OpenAI API (e.g., GPT-4o) to extract and populate the
applicant's name, email, phone, skills, and degree.
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
            'openai',
        ],
    },
    'license': 'LGPL-3',
    'installable': True,
    'application': False,
    'auto_install': False,
}
