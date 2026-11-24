"""Ключи источников данных проекта. Python 3.8+."""

class BusinessData:
    class Raw:
        CALENDAR = "raw.ueex.calendar"
        ANNOUNCEMENT = "raw.ueex.announcement"
        POSITIONS = "raw.ueex.positions"

    class Processed:
        RESULTS = "processed.ueex.gas_export"
