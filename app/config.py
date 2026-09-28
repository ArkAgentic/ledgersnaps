from pydantic import BaseModel


class Settings(BaseModel):
    max_pdf_pages: int = 10
    max_file_mb: int = 25
    max_batch_files: int = 20
    max_batch_total_mb: int = 100
    # Per-job invoice cap (owner-scoped): applies to each job independently.
    max_invoices_per_job: int = 20
    # Queue backend: sqlite (default) | servicebus
    queue_backend: str = "sqlite"


settings = Settings()
