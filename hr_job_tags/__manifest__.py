{
    'name': 'HR Job Tags',
    'version': '17.0.1.0.0',
    'category': 'Human Resources',
    'summary': 'Add tags to Job Positions',
    'description': """
        This module allows you to define and assign tags to Job Positions.
        It adds:

        - A new Job Tag model.
        - A Tags field on the Job Position form.
        - Tags display on the Job Position Kanban view.
    """,
    'author': 'Odoo Expert',
    'depends': ['hr', 'hr_recruitment'],
    'data': [
        'security/ir.model.access.csv',
        'views/hr_job_tag_views.xml',
        'views/hr_job_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}