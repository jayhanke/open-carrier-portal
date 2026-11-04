import sqlite3
from pathlib import Path


def create_npdb(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE company (Company_ID INTEGER PRIMARY KEY, CompanyName TEXT);
            CREATE TABLE Location (
                LocationID INTEGER PRIMARY KEY, Address1 TEXT, Address2 TEXT, City TEXT,
                State TEXT, Zip TEXT, Lat REAL, Lng REAL, Floor TEXT, Suite TEXT
            );
            CREATE TABLE CompanyLocation (
                CompanyLocationID INTEGER PRIMARY KEY, Company_ID INTEGER REFERENCES company,
                LocationID INTEGER REFERENCES Location,
                CommonName TEXT, SiteCode TEXT, RackName TEXT
            );
            CREATE TABLE Device (
                DeviceID INTEGER PRIMARY KEY, DeviceName TEXT,
                CompanyLocationID INTEGER REFERENCES CompanyLocation,
                CompanyID INTEGER REFERENCES company,
                DeviceType TEXT, ObserviumID INTEGER, MgmtIP TEXT, AssetID TEXT
            );
            CREATE TABLE RackUnit (
                RackUnitID INTEGER PRIMARY KEY,
                CompanyLocationID INTEGER REFERENCES CompanyLocation,
                RackUnitName TEXT, DeviceID INTEGER REFERENCES Device
            );
            CREATE TABLE Port (
                PortID INTEGER PRIMARY KEY, PortName TEXT, DeviceID INTEGER REFERENCES Device,
                OwnerCompany INTEGER REFERENCES company, ConnectorType TEXT, ObserviumID INTEGER,
                NNI INTEGER, SubPortOf INTEGER REFERENCES Port, Vlan INTEGER, IPAddress TEXT,
                CardTypeID INTEGER, MACAddress TEXT
            );
            CREATE TABLE Service (
                ServiceID INTEGER PRIMARY KEY, ServiceName TEXT, ServiceType TEXT, ServiceBW TEXT
            );
            CREATE TABLE CircuitLayout (
                CircuitLayoutID INTEGER PRIMARY KEY, CircuitName TEXT,
                PortA INTEGER REFERENCES Port, PortZ INTEGER REFERENCES Port
            );
            CREATE TABLE LitCircuit (
                LitCircuitID INTEGER PRIMARY KEY, LitCircuitName TEXT,
                BillingCompanyID INTEGER REFERENCES company, ServiceID INTEGER REFERENCES Service,
                Description TEXT, CompanyLocationA INTEGER REFERENCES CompanyLocation,
                CompanyLocationZ INTEGER REFERENCES CompanyLocation, ServiceOrderID TEXT,
                CircuitLayoutID INTEGER REFERENCES CircuitLayout, Active INTEGER
            );
            CREATE TABLE CircuitLayoutItem (
                CircuitLayoutItemID INTEGER PRIMARY KEY,
                CircuitLayoutID INTEGER REFERENCES CircuitLayout,
                ItemIndex INTEGER, TableName TEXT, TablePK INTEGER
            );
            CREATE TABLE Patch (
                PatchID INTEGER PRIMARY KEY, PatchType TEXT,
                PortID_A INTEGER REFERENCES Port, PortID_B INTEGER REFERENCES Port,
                PortID_A_sec INTEGER REFERENCES Port, PortID_B_sec INTEGER REFERENCES Port,
                DarkFiberCircuitID INTEGER, OrderDate TEXT, InstallDate TEXT, CancelDate TEXT,
                RemovalDate TEXT, needsRemoval INTEGER, Length INTEGER
            );
            CREATE TABLE PseudoWire (
                PseudoWireID INTEGER PRIMARY KEY, PWID INTEGER,
                PortAID INTEGER REFERENCES Port, PortBID INTEGER REFERENCES Port
            );
            CREATE TABLE NewDarkFiberCircuit (
                NewDarkFiberCircuitID INTEGER PRIMARY KEY, Description TEXT
            );
            INSERT INTO company VALUES (7, 'Example Carrier'), (8, 'Example Partner');
            INSERT INTO Location VALUES
                (10, '100 Main Street', NULL, 'Example City', 'IA', '50000', 42, -93, '2', '201');
            INSERT INTO CompanyLocation VALUES
                (1001, 7, 10, 'Carrier handoff', 'A-SITE', 'Rack A'),
                (1002, 8, 10, 'Partner handoff', 'Z-SITE', 'Rack Z');
            INSERT INTO Device VALUES
                (301, 'edge-a', 1001, 7, 'router', 1301, '192.0.2.1', 'private-asset-a'),
                (302, 'edge-z', 1002, 8, 'router', 1302, '192.0.2.2', 'private-asset-z'),
                (999, 'unshared-device', 1001, 7, 'router', 1999, '192.0.2.99', 'private-asset-x');
            INSERT INTO RackUnit VALUES
                (3001, 1001, 'U20', 301), (3002, 1001, 'U21', 301), (3003, 1002, 'U10', 302);
            INSERT INTO Port VALUES
                (101, 'xe-0/0/1', 301, 7, 'LC', 1101, 1, NULL, 100,
                 '198.51.100.1', 4, 'private-mac-a'),
                (202, 'xe-0/0/2', 302, 8, 'LC', 1202, 1, NULL, 100,
                 '198.51.100.2', 4, 'private-mac-z'),
                (999, 'unshared-port', 999, 7, 'LC', 1999, 0, NULL, NULL,
                 '198.51.100.99', 4, 'private-mac-x');
            INSERT INTO Service VALUES (5, 'Metro Ethernet', 'Ethernet', '10G');
            INSERT INTO CircuitLayout VALUES (42, 'Example path', 101, 202);
            INSERT INTO LitCircuit VALUES
                (926, 'Example circuit', 7, 5, 'Read-only shared circuit', 1001, 1002,
                 'private-order-123', 42, 1),
                (927, 'Unshared circuit', 7, 5, 'Private', 1001, 1002, 'private-order-999', 42, 1);
            INSERT INTO CircuitLayoutItem VALUES
                (1, 42, 10, 'Port', 101), (2, 42, 20, 'Patch', 501),
                (3, 42, 30, 'PW', 601), (4, 42, 40, 'Port', 202);
            INSERT INTO Patch VALUES
                (501, 'fiber', 101, 202, NULL, NULL, NULL, NULL, '2026-01-01', NULL, NULL, 0, 5);
            INSERT INTO PseudoWire VALUES (601, 65000, 101, 202);
            INSERT INTO NewDarkFiberCircuit VALUES (701, 'Example dark fiber segment');
        """)
