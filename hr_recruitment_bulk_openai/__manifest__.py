# -*- coding: utf-8 -*-
{
    'name': "HR Recruitment Bulk OpenAI",
    'summary': """
        Allows bulk uploading CVs on a Job Position to create applicants
        using OpenAI, based on the hr_recruitment_openai addon.""",
    'description': """
This module extends the Job Position form (hr.job) to allow a recruiter
to upload multiple CVs (ir.attachment).
A new button "Add Candidates" appears, which triggers a background
job (using with_delay) to process each CV.

It re-uses the OpenAI assistant logic from hr_recruitment_openai to:
1. Upload each file to OpenAI.
2. Run the assistant to extract data.
3. Parse the JSON response.
4. Create a new Applicant (hr.applicant) for the current job if no applicant with the same name and email exists.
5. Attach the original CV to the new applicant.
6. Send toast notifications to the recruiter on start and completion.
7. Provides a button to clear the uploaded CVs after processing.
    """,
    'author': "alextranduil",
    'website': "https://jito.dev",
    'category': 'Human Resources/Recruitment',
    'version': '17.0.1.0.0',
    'depends': [
        'hr_recruitment',
        'hr_recruitment_openai',  # Depends on custom hr_recruitment_openai existing addon
        'queue_job',  # ADDED: Dependency for background processing
        'bus',          # Dependency for user notifications
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/hr_job_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}


