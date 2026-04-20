"""RQ-callable job entrypoints.

Importing this module from the worker registers every job by virtue of it
being importable by its dotted path (RQ calls jobs by string). The admin
enqueues via ``from app.jobs.domains import register_domain_job`` etc.
"""
from app.jobs.domains import register_domain_job  # noqa: F401
from app.jobs.content import (  # noqa: F401
    generate_homepage_job,
    generate_image_job,
    generate_legal_job,
    generate_post_job,
    launch_site_job,
    publish_post_job,
    refresh_stale_job,
)
