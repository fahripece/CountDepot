"""
Sector definitions for tenant onboarding.

Each sector defines:
  - key:         internal identifier stored in platform.db
  - label:       display name shown on onboarding screen
  - description: one-line description
  - icon:        emoji shown on picker card
  - categories:  list of (name, color, fields[])
                 fields: (label, key, type, placeholder, required)
"""

SECTORS = {

    # ── IT / MSP ──────────────────────────────────────────────────────────────
    "it_msp": {
        "label":       "IT / MSP",
        "description": "Laptops, servers, networking gear, cameras, cabling",
        "icon":        "💻",
        "categories":  [
            ("Laptops", "#1d4ed8", [
                ("CPU",          "cpu",          "text", "e.g. Intel Core i7-1360P", 1),
                ("RAM",          "ram",          "text", "e.g. 16GB DDR5",           1),
                ("Storage",      "storage",      "text", "e.g. 512GB NVMe SSD",      1),
                ("Screen Size",  "screen_size",  "text", 'e.g. 14" FHD',             1),
                ("OS",           "os_type",      "text", "e.g. Windows 11 Pro",      0),
                ("Battery Life", "battery_life", "text", "e.g. 12 hrs",              0),
            ]),
            ("Desktop PCs", "#0369a1", [
                ("CPU",     "cpu",     "text", "e.g. Intel Core i9-13900K", 1),
                ("RAM",     "ram",     "text", "e.g. 32GB DDR5",            1),
                ("Storage", "storage", "text", "e.g. 1TB NVMe SSD",         1),
                ("OS",      "os_type", "text", "e.g. Windows 11 Pro",       0),
            ]),
            ("Servers", "#1e3a5f", [
                ("CPU",             "cpu",     "text", "e.g. 2x Intel Xeon Gold 6248", 1),
                ("RAM",             "ram",     "text", "e.g. 128GB ECC DDR4",          1),
                ("Storage",         "storage", "text", "e.g. 8x1.8TB SAS RAID",        1),
                ("OS / Hypervisor", "os_type", "text", "e.g. VMware ESXi 8.0",         0),
            ]),
            ("Switches", "#0f766e", [
                ("Port Count", "port_count", "number",   "e.g. 24",         1),
                ("Speed",      "speed",      "text",     "e.g. 1Gbps",      1),
                ("PoE Budget", "poe_budget", "text",     "e.g. 195W total", 0),
                ("Has PoE",    "has_poe",    "checkbox", "",                 0),
            ]),
            ("Routers", "#0f766e", [
                ("Throughput",        "throughput",        "text", "e.g. 1Gbps",   1),
                ("Wireless Standard", "wireless_standard", "text", "e.g. Wi-Fi 6", 0),
            ]),
            ("Firewalls", "#b91c1c", [
                ("Throughput", "throughput", "text", "e.g. 1Gbps NGFW", 1),
            ]),
            ("Access Points", "#0891b2", [
                ("Wireless Standard", "wireless_standard", "text",     "e.g. Wi-Fi 6E", 1),
                ("Has PoE",           "has_poe",           "checkbox", "",              0),
            ]),
            ("Cameras", "#7c3aed", [
                ("Resolution", "resolution", "text",     "e.g. 4MP, 8MP, 4K", 1),
                ("Has PoE",    "has_poe",    "checkbox", "",                   0),
            ]),
            ("NVRs / DVRs", "#6d28d9", [
                ("Channel Count",  "channel_count", "number", "e.g. 16",      1),
                ("Max Resolution", "resolution",    "text",   "e.g. 4K",      1),
                ("Storage",        "storage",       "text",   "e.g. 4TB HDD", 0),
            ]),
            ("Phones / VoIP", "#0369a1", [
                ("Has PoE", "has_poe", "checkbox", "", 0),
            ]),
            ("Mobile Devices", "#0f766e", [
                ("OS",      "os_type", "text", "e.g. iOS 17, Android 14",  1),
                ("Storage", "storage", "text", "e.g. 128GB",               0),
                ("IMEI",    "imei",    "text", "15-digit IMEI",             0),
                ("Carrier", "carrier", "text", "e.g. Unlocked, Verizon",   0),
            ]),
            ("Monitors", "#374151", [
                ("Screen Size", "screen_size", "text", 'e.g. 27" 4K IPS', 1),
                ("Resolution",  "resolution",  "text", "e.g. 3840x2160",  1),
            ]),
            ("Cabling", "#92400e", [
                ("Cable Type", "cable_type",     "text", "e.g. Cat6A, Fiber OS2", 1),
                ("Length",     "cable_length",   "text", "e.g. 3ft, 10ft",        1),
                ("Connectors", "connector_type", "text", "e.g. RJ45, LC/LC",      0),
                ("Gauge",      "cable_gauge",    "text", "e.g. 23AWG",            0),
            ]),
            ("UPS / Power", "#b45309", [
                ("Capacity (VA)", "capacity_va",  "text", "e.g. 1500VA / 900W",    1),
                ("Runtime",       "battery_life", "text", "e.g. 10 min full load", 0),
            ]),
            ("Printers / Scanners", "#374151", [
                ("Type", "printer_type", "text", "e.g. Laser, Inkjet, Label", 1),
            ]),
            ("Storage Devices", "#1e3a5f", [
                ("Capacity", "storage", "text", "e.g. 20TB usable", 1),
            ]),
            ("Accessories",  "#6b7280", []),
            ("Consumables",  "#9ca3af", []),
        ],
    },

    # ── Healthcare ────────────────────────────────────────────────────────────
    "healthcare": {
        "label":       "Healthcare",
        "description": "Medical devices, diagnostic equipment, clinical supplies",
        "icon":        "🏥",
        "categories":  [
            ("Diagnostic Equipment", "#0e7490", [
                ("Device Type",      "device_type",      "text", "e.g. MRI, CT, Ultrasound, X-Ray",  1),
                ("Manufacturer",     "manufacturer",     "text", "e.g. Siemens, GE, Philips",        1),
                ("Model #",          "model",            "text", "e.g. SOMATOM X.cite",              1),
                ("Last Serviced",    "last_serviced",    "text", "YYYY-MM-DD",                       0),
                ("Next Service",     "next_service",     "text", "YYYY-MM-DD",                       0),
                ("Last Calibrated",  "last_calibrated",  "text", "YYYY-MM-DD",                       0),
                ("Calibration Due",  "calibration_due",  "text", "YYYY-MM-DD",                       0),
                ("Warranty Expiry",  "warranty_expiry",  "text", "YYYY-MM-DD",                       0),
                ("Room / Dept",      "location",         "text", "e.g. Radiology, ER, ICU",          0),
            ]),
            ("Surgical Equipment", "#be123c", [
                ("Equipment Type",  "equipment_type",  "text", "e.g. Laparoscope, Retractor, Cautery", 1),
                ("Manufacturer",    "manufacturer",    "text", "e.g. Stryker, Olympus, Karl Storz",    0),
                ("Lot / Batch #",   "lot_number",      "text", "Lot or batch number",                  0),
                ("Sterilization",   "sterilization",   "text", "e.g. Autoclave, EtO Gas, Gamma",       0),
                ("Last Sterilized", "last_sterilized", "text", "YYYY-MM-DD",                           0),
                ("Sterile Status",  "sterile_status",  "text", "e.g. Sterile, Non-Sterile, Expired",   0),
            ]),
            ("Critical Care Devices", "#dc2626", [
                ("Device Type",     "device_type",     "text", "e.g. Ventilator, Defibrillator, Anaesthesia Machine", 1),
                ("Manufacturer",    "manufacturer",    "text", "e.g. Dräger, Medtronic, Zoll",         1),
                ("Model #",         "model",           "text", "",                                      1),
                ("Last Serviced",   "last_serviced",   "text", "YYYY-MM-DD",                            0),
                ("Next Service",    "next_service",    "text", "YYYY-MM-DD",                            0),
                ("Last Calibrated", "last_calibrated", "text", "YYYY-MM-DD",                            0),
                ("Calibration Due", "calibration_due", "text", "YYYY-MM-DD",                            0),
                ("Room / Dept",     "location",        "text", "e.g. ICU, Theatre 1, ER",               0),
            ]),
            ("Patient Monitoring", "#0369a1", [
                ("Device Type",     "device_type",     "text",     "e.g. Vitals Monitor, ECG, SpO2, Pulse Ox", 1),
                ("Manufacturer",    "manufacturer",    "text",     "e.g. Mindray, Nihon Kohden, Philips",      0),
                ("Last Calibrated", "last_calibrated", "text",     "YYYY-MM-DD",                               0),
                ("Calibration Due", "calibration_due", "text",     "YYYY-MM-DD",                               0),
                ("Has Wireless",    "has_wireless",    "checkbox", "",                                         0),
                ("Room / Dept",     "location",        "text",     "e.g. Ward 3B, ICU",                        0),
            ]),
            ("Infusion / IV Pumps", "#059669", [
                ("Device Type",   "device_type",   "text", "e.g. IV Pump, Syringe Driver, PCA Pump", 1),
                ("Manufacturer",  "manufacturer",  "text", "e.g. Baxter, BD, B. Braun",              0),
                ("Flow Rate",     "flow_rate",     "text", "e.g. 0.1–999 mL/hr",                     0),
                ("Last Serviced", "last_serviced", "text", "YYYY-MM-DD",                              0),
                ("Next Service",  "next_service",  "text", "YYYY-MM-DD",                              0),
            ]),
            ("Lab Equipment", "#0f766e", [
                ("Equipment Type",  "equipment_type",  "text", "e.g. Centrifuge, Analyser, Incubator, PCR", 1),
                ("Manufacturer",    "manufacturer",    "text", "e.g. Beckman, Roche, Thermo Fisher",        0),
                ("Capacity",        "capacity",        "text", "e.g. 24-sample, 96-well",                   0),
                ("Last Calibrated", "last_calibrated", "text", "YYYY-MM-DD",                                0),
                ("Calibration Due", "calibration_due", "text", "YYYY-MM-DD",                                0),
            ]),
            ("Mobility / Rehab", "#7c3aed", [
                ("Equipment Type",  "equipment_type",  "text", "e.g. Wheelchair, Walker, Hoist, Crutches", 1),
                ("Weight Capacity", "weight_capacity", "text", "e.g. 300 lbs / 136 kg",                    0),
                ("Size",            "size",            "text", "e.g. Standard, Bariatric, Paediatric",     0),
            ]),
            ("Supplies / Consumables", "#9ca3af", [
                ("Category",    "supply_category", "text",   "e.g. PPE, Gloves, Bandages, Syringes", 1),
                ("Unit Count",  "unit_count",      "number", "e.g. 100",                             0),
                ("Lot / Batch #","lot_number",     "text",   "Lot or batch number",                  0),
                ("Expiry Date", "expiry_date",     "text",   "YYYY-MM-DD",                           0),
            ]),
            ("Clinical Workstations", "#1d4ed8", [
                ("Device Type", "device_type",  "text", "e.g. Desktop, Laptop, COW, Thin Client",  1),
                ("Manufacturer","manufacturer", "text", "e.g. Dell, HP, Lenovo",                   0),
                ("Model #",     "model",        "text", "",                                         0),
                ("OS",          "os_type",      "text", "e.g. Windows 10 LTSC, Windows 11 Pro",    0),
                ("Room / Dept", "location",     "text", "e.g. Nurses Station, Ward 2, ER",         0),
            ]),
            ("Furniture / Fixtures", "#92400e", [
                ("Item Type",       "item_type",       "text", "e.g. Hospital Bed, Gurney, IV Stand, Trolley", 1),
                ("Weight Capacity", "weight_capacity", "text", "e.g. 250 kg",                                  0),
                ("Room / Ward",     "location",        "text", "e.g. ICU, Ward 3B, Theatre",                   0),
            ]),
            ("Accessories", "#6b7280", []),
        ],
    },

    # ── Retail / Warehousing ──────────────────────────────────────────────────
    "retail": {
        "label":       "Retail / Warehousing",
        "description": "Products, SKUs, stock levels, warehouse locations",
        "icon":        "📦",
        "categories":  [
            ("Electronics", "#1d4ed8", [
                ("Brand",     "brand",     "text", "e.g. Samsung, Apple",  1),
                ("Model",     "model",     "text", "e.g. Galaxy S24",      1),
                ("UPC / EAN", "upc",       "text", "Barcode number",       1),
                ("Condition", "condition", "text", "New / Refurb / Used",  0),
            ]),
            ("Apparel", "#7c3aed", [
                ("Brand", "brand",  "text", "e.g. Nike, Adidas",  1),
                ("Size",  "size",   "text", "e.g. S, M, L, XL",  1),
                ("Color", "color",  "text", "e.g. Blue, Red",     1),
                ("UPC",   "upc",    "text", "Barcode number",      0),
            ]),
            ("Grocery / Food", "#059669", [
                ("Brand",       "brand",        "text", "e.g. Heinz, Kraft",  1),
                ("SKU",         "vendor_sku",   "text", "Vendor SKU",         1),
                ("Expiry Date", "expiry_date",  "text", "YYYY-MM-DD",         1),
                ("Weight",      "weight",       "text", "e.g. 500g, 1lb",     0),
            ]),
            ("Tools & Hardware", "#b45309", [
                ("Brand", "brand", "text", "e.g. DeWalt, Stanley",  1),
                ("Model", "model", "text", "e.g. DCD777C2",         1),
                ("UPC",   "upc",   "text", "Barcode number",         0),
            ]),
            ("Home & Garden", "#0f766e", [
                ("Brand",      "brand",      "text", "e.g. IKEA, Pottery Barn", 1),
                ("SKU",        "vendor_sku", "text", "Vendor SKU",              1),
                ("Dimensions", "dimensions", "text", "e.g. 30x20x15 cm",        0),
            ]),
            ("Automotive Parts", "#374151", [
                ("OEM Part #", "part_number", "text", "e.g. 1234567",          1),
                ("Fits Make",  "fits_make",   "text", "e.g. Toyota",           1),
                ("Fits Model", "fits_model",  "text", "e.g. Camry 2019-2023",  1),
                ("Fits Year",  "fits_year",   "text", "e.g. 2019-2023",        0),
            ]),
            ("Office Supplies", "#6b7280", [
                ("Brand", "brand",      "text", "e.g. Staples, 3M", 1),
                ("SKU",   "vendor_sku", "text", "Vendor SKU",        1),
            ]),
            ("Seasonal / Clearance", "#b91c1c", [
                ("Season",   "season",   "text", "e.g. Summer 2025",       1),
                ("Discount", "discount", "text", "e.g. 30% off",           0),
            ]),
            ("Consumables",  "#9ca3af", []),
            ("Miscellaneous","#6b7280", []),
        ],
    },

    # ── Education ─────────────────────────────────────────────────────────────
    "education": {
        "label":       "Education",
        "description": "AV equipment, furniture, books, lab gear, IT",
        "icon":        "🎓",
        "categories":  [
            ("Computers & Tablets", "#1d4ed8", [
                ("CPU",     "cpu",     "text", "e.g. Intel Core i5",   1),
                ("RAM",     "ram",     "text", "e.g. 8GB",             1),
                ("Storage", "storage", "text", "e.g. 256GB SSD",       1),
                ("OS",      "os_type", "text", "e.g. Windows 11 EDU",  0),
            ]),
            ("AV Equipment", "#7c3aed", [
                ("Device Type",  "device_type",  "text",     "e.g. Projector, Smartboard, Display", 1),
                ("Resolution",   "resolution",   "text",     "e.g. 4K, 1080p",                     0),
                ("Has Wireless", "has_wireless",  "checkbox", "",                                   0),
            ]),
            ("Networking", "#0f766e", [
                ("Device Type", "device_type", "text",     "e.g. Switch, Access Point, Router", 1),
                ("Port Count",  "port_count",  "number",   "e.g. 24",                           0),
                ("Has PoE",     "has_poe",     "checkbox", "",                                   0),
            ]),
            ("Furniture", "#92400e", [
                ("Item Type",  "item_type",  "text",   "e.g. Desk, Chair, Locker, Cabinet", 1),
                ("Quantity",   "quantity",   "number", "Quantity in set",                    0),
                ("Room / Area","location",   "text",   "e.g. Room 204, Library",             0),
            ]),
            ("Science Lab Equipment", "#059669", [
                ("Equipment Type", "equipment_type", "text", "e.g. Microscope, Centrifuge, Balance", 1),
                ("Grade Level",    "grade_level",    "text", "e.g. High School, College",            0),
            ]),
            ("Books & Textbooks", "#b45309", [
                ("Title",   "book_title", "text", "Book title",           1),
                ("ISBN",    "isbn",       "text", "13-digit ISBN",        1),
                ("Subject", "subject",    "text", "e.g. Math, Biology",   0),
                ("Edition", "edition",    "text", "e.g. 5th Edition",     0),
            ]),
            ("Sports & PE Equipment", "#0891b2", [
                ("Equipment Type", "equipment_type", "text",   "e.g. Balls, Nets, Mats, Goals", 1),
                ("Quantity",       "quantity",        "number", "Units",                         0),
            ]),
            ("Musical Instruments", "#6d28d9", [
                ("Instrument", "instrument",   "text", "e.g. Trumpet, Violin, Guitar", 1),
                ("Brand",      "manufacturer", "text", "e.g. Yamaha, Steinway",        0),
            ]),
            ("Printers / Scanners", "#374151", [
                ("Type", "printer_type", "text", "e.g. Laser, Inkjet, Label", 1),
            ]),
            ("Accessories", "#6b7280", []),
            ("Consumables",  "#9ca3af", []),
        ],
    },

    # ── Construction / Field Services ─────────────────────────────────────────
    "construction": {
        "label":       "Construction / Field Services",
        "description": "Tools, vehicles, heavy equipment, safety gear, materials",
        "icon":        "🏗️",
        "categories":  [
            ("Power Tools", "#b45309", [
                ("Tool Type", "tool_type",    "text", "e.g. Drill, Circular Saw, Grinder", 1),
                ("Brand",     "manufacturer", "text", "e.g. DeWalt, Makita, Milwaukee",    1),
                ("Model",     "model",        "text", "e.g. DCD777C2",                     1),
                ("Voltage",   "voltage",      "text", "e.g. 18V, 20V, 120V",              0),
            ]),
            ("Hand Tools", "#92400e", [
                ("Tool Type", "tool_type",    "text", "e.g. Hammer, Wrench, Level, Chisel", 1),
                ("Brand",     "manufacturer", "text", "e.g. Stanley, Klein, Snap-on",       0),
            ]),
            ("Heavy Equipment", "#374151", [
                ("Equipment Type", "equipment_type", "text",   "e.g. Excavator, Forklift, Crane", 1),
                ("Make",           "manufacturer",   "text",   "e.g. CAT, Komatsu, John Deere",   1),
                ("Model",          "model",          "text",   "e.g. 320 GC",                     1),
                ("Year",           "year",           "text",   "e.g. 2022",                       0),
                ("Operating Hours","operating_hours","text",   "Current hours on meter",           0),
            ]),
            ("Vehicles", "#1e3a5f", [
                ("Make",    "make",    "text", "e.g. Ford, Ram, Chevy",  1),
                ("Model",   "model",  "text", "e.g. F-250, 2500",        1),
                ("Year",    "year",   "text", "e.g. 2023",               1),
                ("VIN",     "vin",    "text", "17-character VIN",         1),
                ("Mileage", "mileage","text", "Current mileage",          0),
                ("License Plate","license_plate","text","e.g. ABC-1234", 0),
            ]),
            ("Safety Gear / PPE", "#b91c1c", [
                ("Item Type", "item_type", "text", "e.g. Hard Hat, Harness, Safety Vest", 1),
                ("Size",      "size",      "text", "e.g. M, L, XL",                      0),
                ("Standard",  "standard",  "text", "e.g. ANSI Z89.1, OSHA 1926",         0),
                ("Expiry",    "expiry_date","text", "YYYY-MM-DD",                         0),
            ]),
            ("Electrical Supplies", "#0369a1", [
                ("Item Type",    "item_type", "text", "e.g. Wire, Breaker, Conduit, Panel", 1),
                ("Specification","spec",      "text", "e.g. 12AWG, 20A, 1\" EMT",           1),
                ("Length / Qty", "length_qty","text", "e.g. 250ft spool, 10 pcs",           0),
            ]),
            ("Plumbing Supplies", "#0891b2", [
                ("Item Type", "item_type", "text", "e.g. Pipe, Valve, Fitting, Trap",  1),
                ("Size",      "size",      "text", "e.g. 3/4\", 2\"",                  1),
                ("Material",  "material",  "text", "e.g. Copper, PVC, PEX, Cast Iron", 0),
            ]),
            ("Materials / Lumber", "#059669", [
                ("Material",   "material",  "text",   "e.g. 2x4 Stud, Plywood, Drywall, Concrete Block", 1),
                ("Dimensions", "dimensions","text",   "e.g. 2x4x8', 4x8 sheet",                          0),
                ("Quantity",   "quantity",  "number", "Units in stock",                                    0),
            ]),
            ("Measurement & Survey", "#6d28d9", [
                ("Tool Type", "tool_type",    "text", "e.g. Total Station, Laser Level, GPS", 1),
                ("Brand",     "manufacturer", "text", "e.g. Leica, Trimble, Topcon",          0),
            ]),
            ("IT / Office", "#1d4ed8", [
                ("Device Type", "device_type",  "text", "e.g. Laptop, Tablet, Printer, Radio", 1),
                ("Brand",       "manufacturer", "text", "e.g. Dell, HP, Motorola",             0),
            ]),
            ("Accessories", "#6b7280", []),
            ("Consumables",  "#9ca3af", []),
        ],
    },

    # ── General / Other ───────────────────────────────────────────────────────
    "general": {
        "label":       "General / Other",
        "description": "Universal categories that work for any business",
        "icon":        "📋",
        "categories":  [
            ("Equipment", "#1d4ed8", [
                ("Item Type", "item_type",    "text", "Equipment type",    1),
                ("Brand",     "manufacturer", "text", "Manufacturer",      0),
                ("Model",     "model",        "text", "Model number",      0),
            ]),
            ("Furniture", "#92400e", [
                ("Item Type", "item_type", "text", "e.g. Desk, Chair, Cabinet", 1),
                ("Location",  "location",  "text", "Room or area",              0),
            ]),
            ("Vehicles", "#374151", [
                ("Make",  "make",  "text", "e.g. Ford, Toyota",   1),
                ("Model", "model", "text", "e.g. Transit, Camry", 1),
                ("Year",  "year",  "text", "e.g. 2023",           1),
                ("VIN",   "vin",   "text", "17-character VIN",    0),
            ]),
            ("IT Equipment", "#0369a1", [
                ("Device Type", "device_type",  "text", "e.g. Laptop, Switch, Printer", 1),
                ("Brand",       "manufacturer", "text", "e.g. Dell, Cisco",             0),
                ("Model",       "model",        "text", "Model number",                 0),
            ]),
            ("Tools", "#b45309", [
                ("Tool Type", "tool_type",    "text", "Tool description", 1),
                ("Brand",     "manufacturer", "text", "Manufacturer",     0),
            ]),
            ("Supplies", "#0f766e", [
                ("Item Type", "item_type", "text", "Description",            1),
                ("Unit",      "unit",      "text", "e.g. each, box, case",   0),
            ]),
            ("Accessories", "#6b7280", []),
            ("Consumables",  "#9ca3af", []),
        ],
    },
}


def get_sector(key):
    return SECTORS.get(key)


def seed_sector_categories(db, sector_key):
    """Wipe existing categories and seed the chosen sector's category set.
    Called during onboarding — replaces any previously seeded categories."""
    sector = SECTORS.get(sector_key)
    if not sector:
        return False

    # Clear out any existing categories and their fields
    db.execute("DELETE FROM category_fields")
    db.execute("DELETE FROM categories")

    for cat_name, color, fields in sector["categories"]:
        try:
            cid = db.execute(
                "INSERT INTO categories (name, color) VALUES (?, ?)",
                [cat_name, color]
            ).lastrowid
            for i, (label, key, ftype, ph, req) in enumerate(fields):
                db.execute(
                    "INSERT INTO category_fields "
                    "(category_id, field_label, field_key, field_type, placeholder, required, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [cid, label, key, ftype, ph, req, i]
                )
        except Exception:
            pass

    db.commit()
    return True
