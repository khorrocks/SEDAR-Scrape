"""SEDAR-Scrape web app: company search, saved companies, a serial download
queue, and the background worker that drives the SEDAR+ browser automation.

The web process (FastAPI) and the worker process (Chrome under Xvfb) share a
database. The web process NEVER touches Chrome; it only reads/writes rows. The
single worker owns the one browser and processes the queue strictly one job at
a time, so a company's full document download (in batches of 30) completes
before the next company starts.
"""

__version__ = "0.2.0"
