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
                ("CPU",            "cpu",     "text", "e.g. 2x Intel Xeon Gold 6248", 1),
                ("RAM",            "ram",     "text", "e.g. 128GB ECC DDR4",          1),
                ("Storage",        "storage", "text", "e.g. 8x1.8TB SAS RAID",        1),
                ("OS / Hypervisor","os_type", "text", "e.g. VMware ESXi 8.0",         0),
            ]),
            ("Switches", "#0f766e", [
                ("Port Count", "port_count", "number", "e.g. 24",          1),
                ("Speed",      "throughput", "text",   "e.g. 1Gbps",       1),
                ("PoE Budget", "poe_budget", "text",   "e.g. 195W total",  0),
                ("Has PoE",    "has_poe",    "checkbox","",                 0),
            ]),
            ("Routers", "#0f766e", [
                ("Throughput",        "throughput",         "text", "e.g. 1Gbps",  1),
                ("Wireless Standard", "wireless_standard",  "text", "e.g. Wi-Fi 6",0),
            ]),
            ("Firewalls", "#b91c1c", [
                ("Throughput", "throughput", "text", "e.g. 1Gbps NGFW", 1),
            ]),
            ("Access Points", "#0891b2", [
                ("Wireless Standard", "wireless_standard", "text",     "e.g. Wi-Fi 6E", 1),
                ("Has PoE",           "has_poe",            "checkbox", "",              0),
            ]),
            ("Cameras", "#7c3aed", [
                ("Resolution", "resolution", "text",     "e.g. 4MP, 8MP, 4K", 1),
                ("Has PoE",    "has_poe",    "checkbox", "",                   0),
            ]),
            ("NVRs / DVRs", "#6d28d9", [
                ("Channel Count",  "port_count",  "number", "e.g. 16",      1),
                ("Max Resolution", "resolution",  "text",   "e.g. 4K",      1),
                ("Storage",        "storage",     "text",   "e.g. 4TB HDD", 0),
            ]),
            ("Phones / VoIP", "#0369a1", [
                ("Has PoE", "has_poe", "checkbox", "", 0),
            ]),
            ("Mobile Devices", "#0f766e", [
                ("OS",       "os_type",     "text", "e.g. iOS 17, Android 14", 1),
                ("Storage",  "storage",     "text", "e.g. 128GB",              0),
                ("IMEI",     "imei",        "text", "15-digit IMEI",           0),
                ("Carrier",  "carrier",     "text", "e.g. Unlocked, Verizon",  0),
            ]),
            ("Monitors", "#374151", [
                ("Screen Size", "screen_size", "text", 'e.g. 27" 4K IPS',  1),
                ("Resolution",  "resolution",  "text", "e.g. 3840x2160",   1),
            ]),
            ("Cabling", "#92400e", [
                ("Cable Type", "cable_type",     "text", "e.g. Cat6A, Fiber OS2", 1),
                ("Length",     "cable_length",   "text", "e.g. 3ft, 10ft",        1),
                ("Connectors", "connector_type", "text", "e.g. RJ45, LC/LC",      0),
                ("Gauge",      "cable_gauge",    "text", "e.g. 23AWG",            0),
            ]),
            ("UPS / Power", "#b45309", [
                ("Capacity (VA)", "throughput",   "text", "e.g. 1500VA / 900W",    1),
                ("Runtime",       "battery_life", "text", "e.g. 10 min full load", 0),
            ]),
            ("Printers / Scanners", "#374151", [
                ("Type", "cable_type", "text", "e.g. Laser, Inkjet, Label", 1),
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
                ("Device Type",    "cable_type",  "text", "e.g. MRI, CT, Ultrasound", 1),
                ("Manufacturer",   "manufacturer","text", "e.g. Siemens, GE",         1),
                ("Model #",        "model",       "text", "e.g. SOMATOM X.cite",      1),
                ("Serial #",       "serial",      "text", "Device serial number",     1),
                ("Last Serviced",  "purchase_date","text","YYYY-MM-DD",               0),
            ]),
            ("Surgical Equipment", "#be123c", [
                ("Equipment Type", "cable_type",  "text", "e.g. Laparoscope, Retractor", 1),
                ("Sterilization",  "os_type",     "text", "e.g. Autoclave, EtO",        0),
            ]),
            ("Patient Monitoring", "#0369a1", [
                ("Device Type",  "cable_type", "text", "e.g. Vitals Monitor, ECG",  1),
                ("Has Wireless", "has_poe",    "checkbox", "",                       0),
            ]),
            ("Infusion / IV", "#059669", [
                ("Device Type", "cable_type", "text", "e.g. IV Pump, Syringe Driver", 1),
                ("Flow Rate",   "throughput", "text", "e.g. 0.1–999 mL/hr",          0),
            ]),
            ("Mobility / Rehab", "#7c3aed", [
                ("Equipment Type", "cable_type", "text", "e.g. Wheelchair, Walker",    1),
                ("Capacity (lbs)", "throughput", "text", "e.g. 300 lbs",              0),
            ]),
            ("Lab Equipment", "#0f766e", [
                ("Equipment Type", "cable_type", "text", "e.g. Centrifuge, Analyzer", 1),
                ("Capacity",       "storage",    "text", "e.g. 24-sample",            0),
            ]),
            ("IT / Workstations", "#1d4ed8", [
                ("CPU",     "cpu",     "text", "e.g. Intel Core i7", 1),
                ("RAM",     "ram",     "text", "e.g. 16GB",          1),
                ("Storage", "storage", "text", "e.g. 512GB SSD",     1),
            ]),
            ("Supplies / Consumables", "#9ca3af", [
                ("Category",   "cable_type", "text", "e.g. PPE, Gloves, Bandages", 1),
                ("Unit Count", "port_count", "number","e.g. 100",                  0),
            ]),
            ("Furniture / Fixtures", "#92400e", [
                ("Item Type", "cable_type", "text", "e.g. Hospital Bed, Gurney", 1),
                ("Capacity",  "throughput", "text", "e.g. 450 lbs",             0),
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
                ("Brand",       "manufacturer","text",   "e.g. Samsung, Apple",  1),
                ("Model",       "model",       "text",   "e.g. Galaxy S24",      1),
                ("UPC/EAN",     "sku",         "text",   "Barcode",              1),
                ("Condition",   "condition",   "text",   "New / Refurb / Used",  0),
            ]),
            ("Apparel", "#7c3aed", [
                ("Brand",  "manufacturer","text",   "e.g. Nike",           1),
                ("Size",   "screen_size", "text",   "e.g. M, L, XL",      1),
                ("Color",  "os_type",     "text",   "e.g. Blue",          0),
                ("UPC",    "sku",         "text",   "Barcode",            0),
            ]),
            ("Grocery / Food", "#059669", [
                ("Brand",     "manufacturer","text",   "e.g. Heinz",        1),
                ("SKU",       "sku",         "text",   "Vendor SKU",        1),
                ("Expiry",    "purchase_date","text",  "YYYY-MM-DD",        0),
                ("Weight",    "throughput",  "text",   "e.g. 500g",        0),
            ]),
            ("Tools & Hardware", "#b45309", [
                ("Brand",    "manufacturer","text",   "e.g. DeWalt",       1),
                ("Model",    "model",       "text",   "e.g. DCD777C2",     1),
                ("UPC",      "sku",         "text",   "Barcode",           0),
            ]),
            ("Home & Garden", "#0f766e", [
                ("Brand",    "manufacturer","text",   "e.g. IKEA",         1),
                ("SKU",      "sku",         "text",   "Vendor SKU",        1),
                ("Dimensions","storage",    "text",   "e.g. 30x20x15 cm",  0),
            ]),
            ("Automotive Parts", "#374151", [
                ("OEM Part #", "sku",         "text", "e.g. 1234567",          1),
                ("Fits Make",  "manufacturer","text", "e.g. Toyota",            1),
                ("Fits Model", "model",       "text", "e.g. Camry 2019-2023",  1),
            ]),
            ("Office Supplies", "#6b7280", [
                ("Brand", "manufacturer","text", "e.g. Staples", 1),
                ("SKU",   "sku",         "text", "Vendor SKU",   1),
            ]),
            ("Seasonal / Clearance", "#b91c1c", [
                ("Season", "os_type", "text", "e.g. Summer 2025", 1),
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
                ("CPU",     "cpu",     "text", "e.g. Intel Core i5", 1),
                ("RAM",     "ram",     "text", "e.g. 8GB",           1),
                ("Storage", "storage", "text", "e.g. 256GB SSD",     1),
                ("OS",      "os_type", "text", "e.g. Windows 11 EDU",0),
            ]),
            ("AV Equipment", "#7c3aed", [
                ("Device Type",  "cable_type",  "text", "e.g. Projector, Screen, Smartboard", 1),
                ("Resolution",   "resolution",  "text", "e.g. 4K, 1080p",                     0),
                ("Has Wireless", "has_poe",     "checkbox", "",                               0),
            ]),
            ("Networking", "#0f766e", [
                ("Device Type", "cable_type", "text",   "e.g. Switch, AP, Router",  1),
                ("Port Count",  "port_count", "number", "e.g. 24",                  0),
                ("Has PoE",     "has_poe",    "checkbox","",                         0),
            ]),
            ("Furniture", "#92400e", [
                ("Item Type",  "cable_type", "text", "e.g. Desk, Chair, Locker",  1),
                ("Quantity",   "port_count", "number","Quantity in set",           0),
                ("Room / Area","shelf",      "text", "e.g. Room 204",             0),
            ]),
            ("Science Lab Equipment", "#059669", [
                ("Equipment Type", "cable_type", "text", "e.g. Microscope, Centrifuge", 1),
                ("Grade Level",    "os_type",    "text", "e.g. High School, College",    0),
            ]),
            ("Books & Textbooks", "#b45309", [
                ("Title",   "name",         "text", "Book title",          1),
                ("ISBN",    "sku",          "text", "13-digit ISBN",       1),
                ("Subject", "cable_type",   "text", "e.g. Math, Biology",  0),
                ("Edition", "model",        "text", "e.g. 5th Edition",    0),
            ]),
            ("Sports & PE Equipment", "#0891b2", [
                ("Equipment Type","cable_type", "text",   "e.g. Balls, Nets, Mats", 1),
                ("Quantity",      "port_count", "number", "Units",                  0),
            ]),
            ("Musical Instruments", "#6d28d9", [
                ("Instrument",  "cable_type",   "text", "e.g. Trumpet, Violin",  1),
                ("Brand",       "manufacturer", "text", "e.g. Yamaha",            0),
            ]),
            ("Printers / Scanners", "#374151", [
                ("Type", "cable_type", "text", "e.g. Laser, Inkjet", 1),
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
                ("Tool Type",    "cable_type",   "text", "e.g. Drill, Saw, Grinder", 1),
                ("Brand",        "manufacturer", "text", "e.g. DeWalt, Makita",      1),
                ("Model",        "model",        "text", "e.g. DCD777C2",            1),
                ("Voltage",      "throughput",   "text", "e.g. 18V, 120V",          0),
            ]),
            ("Hand Tools", "#92400e", [
                ("Tool Type", "cable_type",   "text", "e.g. Hammer, Wrench, Level", 1),
                ("Brand",     "manufacturer", "text", "e.g. Stanley, Klein",        0),
            ]),
            ("Heavy Equipment", "#374151", [
                ("Equipment Type","cable_type",   "text", "e.g. Excavator, Forklift",  1),
                ("Make",          "manufacturer", "text", "e.g. CAT, Komatsu",         1),
                ("Model",         "model",        "text", "e.g. 320 GC",              1),
                ("Year",          "os_type",      "text", "e.g. 2022",               0),
                ("Hours",         "throughput",   "text", "Operating hours",          0),
            ]),
            ("Vehicles", "#1e3a5f", [
                ("Make",    "manufacturer","text", "e.g. Ford, Ram",            1),
                ("Model",   "model",       "text", "e.g. F-250",               1),
                ("Year",    "os_type",     "text", "e.g. 2023",               1),
                ("VIN",     "imei",        "text", "17-character VIN",         1),
                ("Mileage", "throughput",  "text", "Current mileage",          0),
            ]),
            ("Safety Gear / PPE", "#b91c1c", [
                ("Item Type",  "cable_type",   "text", "e.g. Hard Hat, Harness, Vest", 1),
                ("Size",       "screen_size",  "text", "e.g. M, L, XL",              0),
                ("Standard",   "os_type",      "text", "e.g. ANSI Z89.1, OSHA",      0),
            ]),
            ("Electrical Supplies", "#0369a1", [
                ("Item Type",   "cable_type", "text", "e.g. Wire, Breaker, Conduit", 1),
                ("Spec",        "throughput", "text", "e.g. 12AWG, 20A, 1\" EMT",    1),
                ("Length / Qty","storage",    "text", "e.g. 250ft spool",            0),
            ]),
            ("Plumbing Supplies", "#0891b2", [
                ("Item Type", "cable_type", "text", "e.g. Pipe, Valve, Fitting", 1),
                ("Size",      "throughput", "text", "e.g. 3/4\", 2\"",           1),
                ("Material",  "os_type",    "text", "e.g. Copper, PVC, PEX",    0),
            ]),
            ("Materials / Lumber", "#059669", [
                ("Material",   "cable_type", "text",   "e.g. 2x4 Stud, Plywood, Drywall", 1),
                ("Dimensions", "storage",    "text",   "e.g. 2x4x8'",                     0),
                ("Qty",        "port_count", "number", "Units in stock",                   0),
            ]),
            ("Measurement & Survey", "#6d28d9", [
                ("Tool Type", "cable_type",   "text", "e.g. Total Station, Level, GPS", 1),
                ("Brand",     "manufacturer", "text", "e.g. Leica, Trimble",           0),
            ]),
            ("IT / Office", "#1d4ed8", [
                ("Device Type", "cable_type", "text", "e.g. Laptop, Tablet, Printer", 1),
                ("Brand",       "manufacturer","text","e.g. Dell, HP",               0),
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
                ("Type",  "cable_type",   "text", "Equipment type",    1),
                ("Brand", "manufacturer", "text", "Manufacturer",      0),
                ("Model", "model",        "text", "Model number",      0),
            ]),
            ("Furniture", "#92400e", [
                ("Item Type", "cable_type", "text", "e.g. Desk, Chair, Cabinet", 1),
                ("Location",  "shelf",      "text", "Room or area",              0),
            ]),
            ("Vehicles", "#374151", [
                ("Make",  "manufacturer","text","e.g. Ford, Toyota",  1),
                ("Model", "model",       "text","e.g. Transit, Camry",1),
                ("Year",  "os_type",     "text","e.g. 2023",         1),
                ("VIN",   "imei",        "text","17-character VIN",   0),
            ]),
            ("IT Equipment", "#0369a1", [
                ("Device Type","cable_type",   "text","e.g. Laptop, Switch, Printer",1),
                ("Brand",      "manufacturer", "text","e.g. Dell, Cisco",            0),
                ("Model",      "model",        "text","Model number",                0),
            ]),
            ("Tools", "#b45309", [
                ("Tool Type", "cable_type",   "text", "Tool description", 1),
                ("Brand",     "manufacturer", "text", "Manufacturer",     0),
            ]),
            ("Supplies", "#0f766e", [
                ("Item Type", "cable_type", "text", "Description",          1),
                ("Unit",      "throughput", "text", "e.g. each, box, case", 0),
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
