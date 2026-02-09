from dataclasses import dataclass


@dataclass
class EnvelopeLink:
    name: str
    year_month: str
    linked_accounts: str
    scheduled_count: int
    scheduled_total: str

    def __init__(self, name: str, year_month: str, used_mappings: set[str], scheduled_count: int = 0, scheduled_total: str = ""):
        self.name = name
        self.year_month = year_month
        self.linked_accounts = f"^(?:{'|'.join(used_mappings)})$"
        self.scheduled_count = scheduled_count
        self.scheduled_total = scheduled_total

    @property
    def has_scheduled(self) -> bool:
        return self.scheduled_count > 0