/*
mmdvm_status.cpp
================
by DJ0ABR 10.25

this program scans the G4KLX's logfiles extracts QSO
Start/End and Reflector logging and writes this
information into a mysql database
*/

#include <iostream>
#include <fstream>
#include <string>
#include <cstring>
#include <regex>
#include <optional>
#include <sstream>
#include <iomanip>
#include <locale>
#include <cmath>
#include <chrono>
#include <vector>
#include <filesystem>
#include <map>
#include <sys/stat.h>
#include <unistd.h>
#include <thread>
#include <unordered_set>
#include <mariadb/mysql.h> // libmariadb-dev

template <typename... Args>
static void dlog(Args&&... args) {
    (std::cerr << ... << args) << '\n';
}

// ---- Ausgabe-Helfer (Vorwärtsdeklaration) ----
static std::string fmtNum(double v);

struct ParsedResult {
    std::string originalLine;
    std::string mode;      // D-Star, YSF, DMR, oder neue Betriebsart
    std::string startEnd;  // Start, Ende, Mode, Info
    std::string source;    // RF, NET oder "-"
    std::string callsign;  // ggf. leer
    std::optional<int> dgId;
    std::optional<int> slot;
    std::optional<double> durationSec;
    std::optional<double> berPct;
    std::optional<std::string> info;   // für z. B. "Verlinkt zu DCS001 R"
};

struct TransmissionState {
    std::string mode;     // D-Star / YSF / DMR
    std::string source;   // RF / NET
    std::string callsign;
    std::optional<int> dgId;
    std::optional<int> slot;
    std::chrono::system_clock::time_point startTp;
    std::string openedLine; // zur Info
};

struct LocalConfig {
    std::string callsign;
    int         duplex = 0;                 // 0=simplex (default), 1=duplex
    uint64_t    rxFrequency = 0;            // Hz, z.B. 431850000
    uint64_t    txFrequency = 0;            // Hz, z.B. 439450000
    double      latitude  = std::numeric_limits<double>::quiet_NaN();
    double      longitude = std::numeric_limits<double>::quiet_NaN();
    std::string location;                   // z.B. "myCity"
    std::string description;                // z.B. "myCountry"
};

static inline std::optional<double> nullIfNaN(double v) {
    return std::isnan(v) ? std::optional<double>{} : std::optional<double>{v};
}

static inline std::string trim(const std::string& s, bool strip_matching_quotes = false) {
    // 1) Whitespace an beiden Seiten abschneiden (deine Logik)
    size_t a = 0, b = s.size();
    while (a < b && std::isspace((unsigned char)s[a])) ++a;
    while (b > a && std::isspace((unsigned char)s[b-1])) --b;

    std::string t = s.substr(a, b - a);

    // 2) Optional: umschließende "…" oder '…' entfernen – nur wenn beide Seiten gleich sind
    if (strip_matching_quotes && t.size() >= 2) {
        char l = t.front();
        char r = t.back();
        if ((l == '"' && r == '"') || (l == '\'' && r == '\'')) {
            t.erase(t.begin());         // vorne weg
            t.pop_back();               // hinten weg
        }
    }
    return t;
}

// Liest Callsign und Duplex aus /etc/MMDVMHost.ini
// Liest Callsign, Duplex, RX/TX-Frequenzen, GPS und Location/Description aus /etc/MMDVMHost.ini
static LocalConfig readLocalConfig(const std::string& path = "/etc/MMDVMHost.ini") {
    std::ifstream f(path);
    if (!f) {
        dlog("[WARN] Kann ", path, " nicht öffnen – kein lokales Callsign/Duplex bekannt");
        return {};
    }

    LocalConfig cfg;
    std::string line;
    while (std::getline(f, line)) {
        // Kommentare/Leerzeilen überspringen
        std::string raw = trim(line);
        if (raw.empty() || raw[0] == '#' || raw[0] == ';' || raw[0] == '[') continue;

        auto handle = [&](const char* key) -> std::optional<std::string> {
            const size_t len = std::strlen(key);
            if (raw.rfind(key, 0) == 0) {
                return trim(raw.substr(len));
            }
            return std::nullopt;
        };

        if (auto v = handle("Callsign=")) {
            if (cfg.callsign.empty()) {              // << nur erstes Vorkommen übernehmen
                cfg.callsign = *v;
                dlog("[INFO] Lokales Callsign erkannt: ", cfg.callsign);
            }
            continue;
        }
        if (auto v = handle("Duplex=")) {
            try { cfg.duplex = std::stoi(*v); } catch (...) { cfg.duplex = 0; }
            dlog("[INFO] Duplex aus INI: ", cfg.duplex);
            continue;
        }
        if (auto v = handle("RXFrequency=")) {
            try { cfg.rxFrequency = std::stoull(*v); } catch (...) { cfg.rxFrequency = 0; }
            dlog("[INFO] RXFrequency: ", cfg.rxFrequency);
            continue;
        }
        if (auto v = handle("TXFrequency=")) {
            try { cfg.txFrequency = std::stoull(*v); } catch (...) { cfg.txFrequency = 0; }
            dlog("[INFO] TXFrequency: ", cfg.txFrequency);
            continue;
        }
        if (auto v = handle("Latitude=")) {
            try { cfg.latitude = std::stod(*v); } catch (...) { cfg.latitude = std::numeric_limits<double>::quiet_NaN(); }
            dlog("[INFO] Latitude: ", cfg.latitude);
            continue;
        }
        if (auto v = handle("Longitude=")) {
            try { cfg.longitude = std::stod(*v); } catch (...) { cfg.longitude = std::numeric_limits<double>::quiet_NaN(); }
            dlog("[INFO] Longitude: ", cfg.longitude);
            continue;
        }
        if (auto v = handle("Location=")) {
            cfg.location = trim(*v, true);
            dlog("[INFO] Location: ", cfg.location);
            continue;
        }
        if (auto v = handle("Description=")) {
            cfg.description = trim(*v, true);
            dlog("[INFO] Description: ", cfg.description);
            continue;
        }
    }

    if (cfg.callsign.empty())
        dlog("[WARN] Kein Callsign= in ", path, " gefunden");
    return cfg;
}

// Ein gültiges Callsign muss mindestens zwei Buchstaben und eine Zahl enthalten.
static bool isValidCallsign(const std::string& cs) {
    if (cs.size() < 3) return false;  // zu kurz
    int letters = 0, digits = 0;
    for (char c : cs) {
        if (std::isalpha(static_cast<unsigned char>(c))) ++letters;
        if (std::isdigit(static_cast<unsigned char>(c))) ++digits;
    }
    return (letters >= 2 && digits >= 1);
}

struct FileId {
    std::string path;
    uint64_t inode = 0;
    off_t size = 0;
};

struct OffsetEntry {
    uint64_t inode = 0;
    uint64_t offset = 0;
};

static std::string utcDateStr() {
    std::time_t tt = std::time(nullptr);
    std::tm tm{};
    gmtime_r(&tt, &tm);  // <-- UTC!
    std::ostringstream os;
    os << std::put_time(&tm, "%Y-%m-%d");
    return os.str();
}

static std::string localDateStr() {  // optional, als Fallback
    std::time_t tt = std::time(nullptr);
    std::tm tm{};
    localtime_r(&tt, &tm);
    std::ostringstream os;
    os << std::put_time(&tm, "%Y-%m-%d");
    return os.str();
}

static std::vector<std::string> logsForDir(const std::string& dir) {
    const std::string utc = utcDateStr();
    const std::string loc = localDateStr();
    auto build = [&](const std::string& d){
        return std::vector<std::string>{
            (std::filesystem::path(dir)/("DMRGateway-"+d+".log")).string(),
            (std::filesystem::path(dir)/("MMDVM-"+d+".log")).string(),
            (std::filesystem::path(dir)/("YSFGateway-"+d+".log")).string(),
            (std::filesystem::path(dir)/"NXDNGateway.log").string()
        };
    };
    auto utcSet = build(utc);
    auto locSet = build(loc);

    std::vector<std::string> out; out.reserve(3);
    for (size_t i=0;i<utcSet.size();++i) {
        if (std::filesystem::exists(utcSet[i])) out.push_back(utcSet[i]);
        else if (std::filesystem::exists(locSet[i])) out.push_back(locSet[i]);
        else out.push_back(utcSet[i]); // fallback: UTC-Name beobachten
    }
    // Each USB board has its own log. Discover today's files on every scan,
    // including boards connected after the monitor started.
    std::error_code error;
    const std::regex boardLog(R"(^MMDVM-board-[a-f0-9]{12}-(\d{4}-\d{2}-\d{2})\.log$)");
    for (const auto& entry : std::filesystem::directory_iterator(dir, error)) {
        std::smatch match;
        const auto name = entry.path().filename().string();
        if (std::regex_match(name, match, boardLog) && (match[1] == utc || match[1] == loc))
            out.push_back(entry.path().string());
    }
    return out;
}

static std::string offsetsPath() {
    std::string base = "/tmp";
    try {
        std::filesystem::create_directories(base);
    } catch (...) {
        // Ignorieren — /tmp existiert eh
    }
    return base + "/logparse.offsets";
}

static std::map<std::string, OffsetEntry> loadOffsets() {
    std::map<std::string, OffsetEntry> m;
    std::ifstream in(offsetsPath());
    if (!in) return m;
    std::string path; uint64_t inode, offset;
    while (in >> std::ws && std::getline(in, path)) {
        if (path.rfind("#", 0) == 0 || path.empty()) continue; // Kommentare
        // Format: <path>\t<inode>\t<offset>
        std::istringstream ls(path);
        std::string realPath; std::string inodeStr; std::string offStr;
        if (std::getline(ls, realPath, '\t') &&
            std::getline(ls, inodeStr, '\t') &&
            std::getline(ls, offStr)) {
            try {
                inode = std::stoull(inodeStr);
                offset = std::stoull(offStr);
                m[realPath] = OffsetEntry{inode, offset};
            } catch (...) {}
        }
    }
    return m;
}

static void saveOffsets(const std::map<std::string, OffsetEntry>& m) {
    std::ofstream out(offsetsPath(), std::ios::trunc);
    for (const auto& [path, ent] : m) {
        out << path << '\t' << ent.inode << '\t' << ent.offset << '\n';
    }
}

static std::optional<FileId> statFile(const std::string& path) {
    struct stat st{};
    if (::stat(path.c_str(), &st) != 0) return std::nullopt;
    return FileId{path, static_cast<uint64_t>(st.st_ino), st.st_size};
}

static std::vector<std::string> defaultLogPaths() {
    return logsForDir("/var/log/mmdvm");
}


static inline bool starts_with(const std::string& s, const char* pfx) {
    size_t n = std::strlen(pfx);
    return s.size() >= n && std::memcmp(s.data(), pfx, n) == 0;
}

class LogParser {
public:
    explicit LogParser(const LocalConfig& lc = {})
        : localCallsign(lc.callsign),
          ignoreSelfOnNET(lc.duplex == 1) {
        // --- D-Star ---
        rx.dstar_netStart = std::regex(R"(D-Star,\s+received\s+network\s+header\s+from\s+(\S+))");
        rx.dstar_netEnd   = std::regex(R"(D-Star,\s+received\s+network\s+end\s+of\s+transmission\s+from\s+(\S+).*?,\s*([\d.]+)\s+seconds,.*?BER:\s*([\d.]+)%)");
        rx.dstar_rfStart  = std::regex(R"(D-Star,\s+received\s+RF\s+(?:header|late entry)\s+from\s+(\S+))");
        rx.dstar_rfEnd    = std::regex(R"(D-Star,\s+received\s+RF\s+end\s+of\s+transmission\s+from\s+(\S+).*?,\s*([\d.]+)\s+seconds,\s*BER:\s*([\d.]+)%)");

        // --- YSF ---
        rx.ysf_netStartA  = std::regex(R"(YSF,\s+received\s+network\s+data\s+from\s+(\S+)\s+to\s+DG-ID\s+(\d+)\s+at\s+\S+)");
        rx.ysf_netStartB  = std::regex(R"(YSF,\s+received\s+network\s+data\s+from\s+(\S+)\s+to\s+DG-ID\s+(\d+))");
        rx.ysf_netEndA    = std::regex(R"(YSF,\s+network\s+watchdog\s+has\s+expired,\s*([\d.]+)\s+seconds(?:,[^,]*)?,\s*BER:\s*([\d.]+)%)");
        rx.ysf_netEndB = std::regex(
            R"(YSF,\s+received\s+network\s+end\s+of\s+transmission\s+from\s+(\S+)\s+to\s+DG-ID\s+(\d+),\s*([\d.]+)\s+seconds(?:,.*?BER:\s*([\d.]+)%)?)"
        );
        rx.ysf_rfStart    = std::regex(R"(YSF,\s+received\s+RF\s+header\s+from\s+(\S+)\s+to\s+DG-ID\s+(\d+))");
        rx.ysf_rfEnd = std::regex(
            R"(YSF,\s+received\s+RF\s+end\s+of\s+transmission\s+from\s+(\S+)\s+to\s+DG-ID\s+(\d+),\s*([\d.]+)\s+seconds(?:,.*?BER:\s*([\d.]+)%)?)"
        );

        // --- DMR ---
        rx.dmr_netStart   = std::regex(R"(DMR\s+Slot\s+(\d+),\s+received\s+network\s+voice\s+header\s+from\s+(\S+)\s+to\s+TG\s+(\d+))");
        rx.dmr_netEnd     = std::regex(R"(DMR\s+Slot\s+(\d+),\s+received\s+network\s+end\s+of\s+voice\s+transmission\s+from\s+(\S+)\s+to\s+TG\s+(\d+),\s*([\d.]+)\s+seconds,.*?BER:\s*([\d.]+)%)");
        rx.dmr_rfStart    = std::regex(R"(DMR\s+Slot\s+(\d+),\s+received\s+RF\s+voice\s+header\s+from\s+(\S+)\s+to\s+TG\s+(\d+))");
        rx.dmr_rfEnd      = std::regex(R"(DMR\s+Slot\s+(\d+),\s+received\s+RF\s+end\s+of\s+voice\s+transmission\s+from\s+(\S+)\s+to\s+TG\s+(\d+),\s*([\d.]+)\s+seconds,\s*BER:\s*([\d.]+)%)");

        // --- Allgemein ---
        rx.nxdn_start = std::regex(R"(NXDN,\s+received\s+(RF|network)\s+(?:header|late entry|transmission)\s+from\s+(\S+)\s+to\s+(?:TG\s+)?(\d+))");
        rx.nxdn_end = std::regex(R"(NXDN,\s+received\s+(RF|network)\s+end of transmission\s+from\s+(\S+)\s+to\s+(?:TG\s+)?(\d+),\s*([\d.]+)\s+seconds(?:,\s*BER:\s*([\d.]+)%)?)");
        rx.nxdn_lost = std::regex(R"(NXDN,\s+transmission lost from\s+(\S+)\s+to\s+(?:TG\s+)?(\d+),\s*([\d.]+)\s+seconds,\s*BER:\s*([\d.]+)%)");
        rx.nxdn_watchdog = std::regex(R"(NXDN,\s+network watchdog has expired,\s*([\d.]+)\s+seconds)");
        rx.nxdn_link = std::regex(R"(Switched to reflector (\d+) (?:due to (?:RF|network) activity|by remote command))");
        rx.modeIdle       = std::regex(R"(Mode\s+set\s+to\s+Idle)");
        rx.timestamp      = std::regex(R"(^\s*[A-Z]:\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\.(\d{3}))");

        rx.modeSet = std::regex(R"(Mode\s+set\s+to\s+([A-Za-z0-9\-]+))");
        rx.ysf_linked_to    = std::regex(R"(Linked\s+to\s+([^\r\n\"]+))");
        rx.dmr_master_login = std::regex(R"((\S+),\s+Logged\s+into\s+the\s+master\s+successfully)");

        rx.dstar_slowdata_text   = std::regex(R"(D-Star,\s+network\s+slow\s+data\s+text\s*=\s*\"([^\"]+)\")");
        rx.dstar_link_status_set = std::regex(R"(D-Star\s+link\s+status\s+set\s+to\s*\"([^\"]+)\")");
    }

    // Gibt bei relevanter Zeile ein ParsedResult zurück; sonst nullopt.
    std::optional<ParsedResult> processLine(const std::string& line, bool nxdnGateway = false) {
        auto ts = extractTimestamp(line);

        // Betriebsart-Wechsel ("Mode set to <Mode>")
        {
            std::smatch m;
            if (std::regex_search(line, m, rx.modeSet)) {
                std::string newMode = m[1].str();

                // Idle closes every channel; changing protocol closes incompatible sessions.
                for (auto it = open_.begin(); it != open_.end();) {
                    int channel = it++->first;
                    if (newMode == "Idle" ||
                        (newMode == "DMR" ? channel == 0 : channel != 0)) {
                        if (auto end = forcedEndIfOpen(line, ts, channel)) pending_.push_back(*end);
                    }
                }

                // Eigenes Ergebnis-Objekt für den Mode-Wechsel
                ParsedResult res;
                res.originalLine = line;
                res.mode = newMode;          // neue Betriebsart
                res.startEnd = "Mode";
                res.source = "-";
                res.callsign = "";
                res.dgId = std::nullopt;
                res.durationSec = std::nullopt;
                res.berPct = std::nullopt;
                res.info = std::nullopt;

                return res;
            }
        }

        // D-Star link announcements may be English or German. Consume the whole
        // quoted message here so it cannot fall through into the YSF rule.
        {
            std::smatch m;
            if (std::regex_search(line, m, rx.dstar_slowdata_text) ||
                std::regex_search(line, m, rx.dstar_link_status_set)) {
                const std::string text = trim(m[1].str());
                std::string reflector;
                if (starts_with(text, "Linked to ")) {
                    reflector = trim(text.substr(10));
                } else if (starts_with(text, "Verlinkt zu ")) {
                    reflector = trim(text.substr(12));
                } else if (text != "Not linked" && text != "Nicht verlinkt" && text != "Unlinked") {
                    return std::nullopt; // User slow-data text is not a link announcement.
                }
                ParsedResult res;
                res.originalLine = line;
                res.mode = "D-Star";
                res.startEnd = "Info";
                res.source = "NET";
                res.info = reflector;
                return res;
            }
        }

        // 2) YSF: "Linked to <Reflector>"
        {
            std::smatch m;
            const bool linked = std::regex_search(line, m, rx.nxdn_link);
            if (nxdnGateway && (linked || line.find("NXDNGateway is stopping") != std::string::npos ||
                (line.find("NXDNGateway-") != std::string::npos && line.find(" is starting") != std::string::npos) ||
                line.find("Unlinking from ") != std::string::npos)) {
                ParsedResult res;
                res.originalLine = line;
                res.mode = "NXDN";
                res.startEnd = "Info";
                res.info = linked ? "TG " + m[1].str() : "";
                return res;
            }
        }

        {
            std::smatch m;
            if (line.find("D-Star") == std::string::npos &&
                std::regex_search(line, m, rx.ysf_linked_to)) {
                ParsedResult res;
                res.originalLine = line;
                res.mode = "YSF";
                res.startEnd = "Info";
                res.source = "-";
                res.callsign = "";
                res.dgId = std::nullopt;
                res.durationSec = std::nullopt;
                res.berPct = std::nullopt;
                res.info = std::string("Linked to ") + trim(m[1].str());
                return res;
            }
        }

        // Disconnect-Meldungen als Info erzeugen
        if (line.find("Disconnect by remote command") != std::string::npos ||
            line.find("Closing YSF network connection") != std::string::npos) {

            ParsedResult res;
            res.originalLine = line;
            res.mode = "YSF";
            res.startEnd = "Info";
            res.source = "-";
            res.callsign = "";
            res.dgId = std::nullopt;
            res.durationSec = std::nullopt;
            res.berPct = std::nullopt;
            // Marker, den wir später in handleParsed auswerten
            res.info = std::string("DISCONNECTED");
            return res;
        }

        // 3) DMR: "<ServerName>, Logged into the master successfully"
        {
            std::smatch m;
            if (std::regex_search(line, m, rx.dmr_master_login)) {
                ParsedResult res;
                res.originalLine = line;
                res.mode = "DMR";
                res.startEnd = "Info";
                res.source = "-";
                res.callsign = "";
                res.dgId = std::nullopt;
                res.durationSec = std::nullopt;
                res.berPct = std::nullopt;
                res.info = std::string("Logged into master: ") + m[1].str(); // z. B. "Logged into master: BM_2621_Germany"
                return res;
            }
        }

        // --- D-Star ---
        {
            std::smatch m;
            if (std::regex_search(line, m, rx.dstar_netStart)) {
                return handleStart(line, ts, "D-Star", "NET", m[1].str(), std::nullopt);
            }
            if (std::regex_search(line, m, rx.dstar_netEnd)) {
                return handleEnd(line, ts, "D-Star", "NET", m[1].str(), std::nullopt,
                                 toDouble(m[2].str()), toDouble(m[3].str()));
            }
            if (std::regex_search(line, m, rx.dstar_rfStart)) {
                return handleStart(line, ts, "D-Star", "RF", m[1].str(), std::nullopt);
            }
            if (std::regex_search(line, m, rx.dstar_rfEnd)) {
                return handleEnd(line, ts, "D-Star", "RF", m[1].str(), std::nullopt,
                                 toDouble(m[2].str()), toDouble(m[3].str()));
            }
        }

        // --- YSF ---
        {
            std::smatch m;
            if (std::regex_search(line, m, rx.ysf_netStartA) ||
                std::regex_search(line, m, rx.ysf_netStartB)) {
                std::string cs = m[1].str();
                int dg = std::stoi(m[2].str());
                return handleStart(line, ts, "YSF", "NET", cs, dg);
            }
            if (std::regex_search(line, m, rx.ysf_netEndB)) {
                std::string cs = m[1].str();
                int dg = std::stoi(m[2].str());
                double dur = toDouble(m[3].str());
                std::optional<double> ber = (m.size() > 4 && m[4].matched) ? std::optional<double>(toDouble(m[4].str())) : std::nullopt;
                return handleEnd(line, ts, "YSF", "NET", cs, dg, dur, ber);
            }
            if (std::regex_search(line, m, rx.ysf_netEndA)) {
                // Watchdog-Ende ohne Callsign/DG-ID
                return handleEnd(line, ts, "YSF", "NET", "", std::nullopt, toDouble(m[1].str()), toDouble(m[2].str()));
            }
            if (std::regex_search(line, m, rx.ysf_rfStart)) {
                std::string cs = m[1].str();
                int dg = std::stoi(m[2].str());
                return handleStart(line, ts, "YSF", "RF", cs, dg);
            }
            if (std::regex_search(line, m, rx.ysf_rfEnd)) {
                std::string cs = m[1].str();
                int dg = std::stoi(m[2].str());
                double dur = toDouble(m[3].str());
                std::optional<double> ber = (m.size() > 4 && m[4].matched) ? std::optional<double>(toDouble(m[4].str())) : std::nullopt;
                return handleEnd(line, ts, "YSF", "RF", cs, dg, dur, ber);
            }
        }

        // --- DMR ---
        {
            std::smatch m;
            if (std::regex_search(line, m, rx.dmr_netStart)) {
                int slot = std::stoi(m[1].str());
                std::string cs = m[2].str();
                int tg = std::stoi(m[3].str());
                return handleStart(line, ts, "DMR", "NET", cs, tg, slot);
            }
            if (std::regex_search(line, m, rx.dmr_netEnd)) {
                int slot = std::stoi(m[1].str());
                std::string cs = m[2].str();
                int tg = std::stoi(m[3].str());
                double dur = toDouble(m[4].str());
                double ber = toDouble(m[5].str());
                return handleEnd(line, ts, "DMR", "NET", cs, tg, dur, ber, slot);            }
            if (std::regex_search(line, m, rx.dmr_rfStart)) {
                int slot = std::stoi(m[1].str());
                std::string cs = m[2].str();
                int tg = std::stoi(m[3].str());
                return handleStart(line, ts, "DMR", "RF", cs, tg, slot);
            }
            if (std::regex_search(line, m, rx.dmr_rfEnd)) {
                int slot = std::stoi(m[1].str());
                std::string cs = m[2].str();
                int tg = std::stoi(m[3].str());
                double dur = toDouble(m[4].str());
                double ber = toDouble(m[5].str());
                return handleEnd(line, ts, "DMR", "RF", cs, tg, dur, ber, slot);            }
        }

        // NXDN shares the non-DMR radio channel, with no invented BER on NET.
        {
            std::smatch m;
            if (std::regex_search(line, m, rx.nxdn_start))
                return handleStart(line, ts, "NXDN", m[1] == "RF" ? "RF" : "NET", m[2], std::stoi(m[3]));
            if (std::regex_search(line, m, rx.nxdn_end))
                return handleEnd(line, ts, "NXDN", m[1] == "RF" ? "RF" : "NET", m[2], std::stoi(m[3]),
                                 toDouble(m[4]), m[5].matched ? std::optional<double>(toDouble(m[5])) : std::nullopt);
            if (std::regex_search(line, m, rx.nxdn_lost))
                return handleEnd(line, ts, "NXDN", "RF", m[1], std::stoi(m[2]), toDouble(m[3]), toDouble(m[4]));
            if (std::regex_search(line, m, rx.nxdn_watchdog))
                return handleEnd(line, ts, "NXDN", "NET", "", std::nullopt, toDouble(m[1]), std::nullopt);
        }

        // sonst nicht relevant
        return std::nullopt;
    }

    // Am Ende der Datei aufrufen, um eine offene Übertragung sauber zu schließen (falls gewünscht).
    std::optional<ParsedResult> flushAtEof(const std::string& lastLine) {
        if (open_.empty()) return std::nullopt;
        return forcedEndIfOpen(lastLine, std::nullopt, open_.begin()->first);
    }

    // Abruf der ggf. aufgelaufenen "erzwungenen Ende"-Ergebnisse vor einem Start
    std::vector<ParsedResult> takePending() {
        auto out = pending_;
        pending_.clear();
        return out;
    }

private:
    std::string localCallsign;
    bool ignoreSelfOnNET = false;

    struct Regexes {
        // D-Star
        std::regex dstar_netStart, dstar_netEnd, dstar_rfStart, dstar_rfEnd;
        // YSF (Varianten)
        std::regex ysf_netStartA, ysf_netStartB, ysf_netEndA, ysf_netEndB, ysf_rfStart, ysf_rfEnd;
        // DMR
        std::regex dmr_netStart, dmr_netEnd, dmr_rfStart, dmr_rfEnd;
        std::regex nxdn_start, nxdn_end, nxdn_lost, nxdn_watchdog, nxdn_link;
        // Allgemein
        std::regex modeIdle;
        std::regex timestamp;
        std::regex modeSet;              // "Mode set to <Mode>"
        std::regex dstar_slowdata_text;  // D-Star slow data text = "...."
        std::regex ysf_linked_to;      // "Linked to <Reflector>"
        std::regex dmr_master_login;   // "<ServerName>, Logged into the master successfully"
        std::regex dstar_link_status_set;  // D-Star link status set to "Verlinkt zu DCS001 R"
    } rx;

    // Channel 0 is D-Star/YSF; DMR TS1 and TS2 have independent sessions.
    std::map<int, TransmissionState> open_;
    std::vector<ParsedResult> pending_;

    static double toDouble(const std::string& s) {
        return std::stod(s);
    }

    static std::optional<std::chrono::system_clock::time_point>
    extractTimestamp(const std::string& line) {
        static const std::regex tsre(R"(^\s*[A-Z]:\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\.(\d{3}))");
        std::smatch m;
        if (!std::regex_search(line, m, tsre)) return std::nullopt;
        std::string tmain = m[1].str();
        int ms = std::stoi(m[2].str());

        std::tm tm = {};
        std::istringstream iss(tmain);
        iss.imbue(std::locale::classic());
        iss >> std::get_time(&tm, "%Y-%m-%d %H:%M:%S");
        if (iss.fail()) return std::nullopt;

        std::time_t tt = std::mktime(&tm);
        if (tt == -1) return std::nullopt;

        auto tp = std::chrono::system_clock::from_time_t(tt) + std::chrono::milliseconds(ms);
        return tp;
    }

    // Erzwungenes Ende (Idle oder Start überschreibt offene Session)
    std::optional<ParsedResult>
    forcedEndIfOpen(const std::string& line,
                    const std::optional<std::chrono::system_clock::time_point>& ts,
                    int channel) {
        auto it = open_.find(channel);
        if (it == open_.end()) return std::nullopt;
        const auto& session = it->second;

        ParsedResult res;
        res.originalLine = line;
        res.mode = session.mode;
        res.startEnd = "Ende";
        res.source = session.source;
        res.callsign = session.callsign;
        res.dgId = session.dgId;
        res.slot = session.slot;

        if (ts.has_value()) {
            auto d = std::chrono::duration_cast<std::chrono::milliseconds>(ts.value() - session.startTp).count();
            res.durationSec = d / 1000.0;
        } else {
            res.durationSec = std::nullopt;
        }
        res.berPct = std::nullopt; // nicht im Log
        open_.erase(it);
        return res;
    }

    // Helper: Callsign säubern (Suffixe /<...> oder Spaces entfernen)
    static std::string sanitizeCallsign(const std::string& in) {
        std::string s = trim(in);
        size_t p = s.find_first_of("/ ");
        if (p != std::string::npos) s = s.substr(0, p);
        return s;
    }

    static bool validIdentity(const std::string& mode, const std::string& identity) {
        if (isValidCallsign(identity)) return true;
        return mode == "NXDN" && !identity.empty() && identity.size() <= 5 &&
               identity.find_first_not_of("0123456789") == std::string::npos &&
               std::stoi(identity) > 0 && std::stoi(identity) <= 65535;
    }

    std::optional<ParsedResult>
    handleStart(const std::string& line,
                const std::optional<std::chrono::system_clock::time_point>& ts,
                const std::string& mode,
                const std::string& source,
                const std::string& callsign,
                std::optional<int> dgId,
                std::optional<int> slotId = std::nullopt) {
                    
        std::string cs = sanitizeCallsign(callsign);
        if (!cs.empty() && !validIdentity(mode, cs)) return std::nullopt;

        // ignore if it's our own callsign (with optional -suffix)
        auto isSelf = [&](const std::string& cs) -> bool {
            if (localCallsign.empty() || cs.empty()) return false;
            if (cs.rfind(localCallsign, 0) == 0) return true; // beginnt mit eigenem Callsign
            return false;
        };
        // Nur Netzwerk-Echos unterdrücken – RF mit eigenem Call behalten
        if (isSelf(cs) && source == "NET" && ignoreSelfOnNET) return std::nullopt;

        const int channel = mode == "DMR" ? slotId.value_or(0) : 0;
        for (auto it = open_.begin(); it != open_.end();) {
            int existing = it++->first;
            if (existing == channel || channel == 0 || existing == 0) {
                if (auto end = forcedEndIfOpen(line, ts, existing)) pending_.push_back(*end);
            }
        }

        auto stp = ts.value_or(std::chrono::system_clock::now());
        open_[channel] = TransmissionState{mode, source, cs, dgId, slotId, stp, line};

        ParsedResult res;
        res.originalLine = line;
        res.mode = mode;
        res.startEnd = "Start";
        res.source = source;
        res.callsign = cs;
        res.dgId = dgId;
        res.slot = slotId;
        res.durationSec = std::nullopt;
        res.berPct = std::nullopt;

        return res;
    }

    std::optional<ParsedResult>
    handleEnd(const std::string& line,
            const std::optional<std::chrono::system_clock::time_point>& /*ts*/,
            const std::string& mode,
            const std::string& source,
            const std::string& callsign,
            std::optional<int> dgId,
            std::optional<double> durationSec,
            std::optional<double> berPct,
            std::optional<int> slotId = std::nullopt) {

        std::string cs = sanitizeCallsign(callsign);
        if (!cs.empty() && !validIdentity(mode, cs)) return std::nullopt;

        // ignore if it's our own callsign (with optional -suffix)
        auto isSelf = [&](const std::string& cs) -> bool {
            if (localCallsign.empty() || cs.empty()) return false;
            if (cs.rfind(localCallsign, 0) == 0) return true; // beginnt mit eigenem Callsign
            return false;
        };
        if (isSelf(cs) && source == "NET" && ignoreSelfOnNET) return std::nullopt;

        // Falls das Ende kein Rufzeichen/DG-ID liefert (z. B. YSF Watchdog),
        // nimm die Werte aus der offenen Übertragung, wenn vorhanden.
        std::string outCallsign = cs;
        std::optional<int> outDgId = dgId;
        std::optional<int> outSlot = slotId;

        const int channel = mode == "DMR" ? slotId.value_or(0) : 0;
        auto it = open_.find(channel);
        if (it != open_.end()) {
            const auto& session = it->second;
            if (session.mode != mode || session.source != source ||
                (!cs.empty() && cs != session.callsign) ||
                (dgId && session.dgId && dgId != session.dgId)) {
                return std::nullopt; // A late end must not close another call.
            }
            if (outCallsign.empty()) outCallsign = session.callsign;
            if (!outDgId.has_value()) outDgId = session.dgId;
            if (!outSlot.has_value()) outSlot = session.slot;
            open_.erase(it);
        }

        ParsedResult res;
        res.originalLine = line;
        res.mode = mode;
        res.startEnd = "Ende";
        res.source = source;
        res.callsign = outCallsign;
        res.dgId = outDgId;
        res.slot = outSlot;
        res.durationSec = durationSec;
        res.berPct = berPct;
        return res;
    }

};

// ---- Ausgabe-Helfer ----
static std::string fmtNum(double v) {
    std::ostringstream os;
    os.setf(std::ios::fixed);
    os << std::setprecision(3) << v;
    std::string s = os.str();
    while (!s.empty() && s.back()=='0') s.pop_back();
    if (!s.empty() && s.back()=='.') s.pop_back();
    return s;
}

static void printResult(const ParsedResult& r) {
    std::cout << "ZEILE:   " << r.originalLine << "\n";
    std::cout << "ANALYSE: " << r.startEnd;

    // Quelle/Mode nur ausgeben, wenn sinnvoll
    if (!r.source.empty() && r.source != "-") std::cout << ", " << r.source;
    if (!r.mode.empty()) std::cout << ", " << r.mode;

    // Callsign/DG-ID nur wenn vorhanden/sinnvoll
    if (!r.callsign.empty())
        std::cout << ", Callsign=" << (r.callsign.empty() ? "-" : r.callsign);
    if (r.dgId.has_value())  std::cout << ", DG-ID=" << *r.dgId;
    if (r.slot.has_value())  std::cout << ", Slot="  << *r.slot;

    // Dauer/BER wenn vorhanden
    if (r.durationSec.has_value())
        std::cout << ", Dauer[s]=" << fmtNum(*r.durationSec);
    if (r.berPct.has_value())
        std::cout << ", BER[%]=" << fmtNum(*r.berPct);

    // Info-Text (z. B. Verlinkt zu DCS001 R)
    if (r.info.has_value())
        std::cout << ", Info=\"" << *r.info << "\"";

    std::cout << "\n";
}


[[maybe_unused]] static void printStatus(const std::map<std::string, OffsetEntry>& offsets,
                        const std::map<std::string, uint64_t>& lastReadCounts) {
    std::cout << "\033[2K\r"; // Zeile löschen (TTY-freundlich)
    std::cout.flush();

    std::cout << "[STATUS] Watching " << offsets.size() << " log files: ";
    bool first = true;
    for (const auto& [path, off] : offsets) {
        if (!first) std::cout << " | ";
        std::cout << std::filesystem::path(path).filename().string();
        first = false;
    }

    std::cout << "\n";

    for (const auto& [path, count] : lastReadCounts) {
        std::cout << "  " << std::filesystem::path(path).filename().string()
                  << " +" << count << " lines" << std::endl;
    }

    std::cout.flush();
}

class Database {
public:
    Database()
    : host("localhost"), user("mmdvm"), pass(""), name("mmdvmdb"),
      port(0), unix_socket("/run/mysqld/mysqld.sock"),
      conn(nullptr),
      st_upsert_status(nullptr), st_insert_lastheard(nullptr),
      st_upsert_reflector_dstar(nullptr), st_upsert_reflector_fusion(nullptr), st_upsert_reflector_dmr(nullptr), st_upsert_reflector_nxdn(nullptr)
       {
    }

    ~Database() {
        destroy_statements();
        if (conn) { mysql_close(conn); conn = nullptr; }
    }

    bool ensure_conn() {
        if (!conn) return connect();
        if (mysql_ping(conn) != 0) {
            dlog("[DB  ] ping failed: ", mysql_error(conn), " -> reconnect");
            return connect();
        }
        return true;
    }

    // ---- High-level Actions ----
void upsertStatus(const std::string& mode,
                  const std::string& callsign,
                  const std::optional<int>& dgid,
                  const std::optional<int>& slot,
                  const std::optional<std::string>& source,
                  bool active,
                  const std::optional<double>& ber,
                  const std::optional<double>& duration) {
        if (!ensure_conn() || !st_upsert_status) return;

        MYSQL_BIND b[9]{};
        int rowId = 1; // Legacy summary plus one persistent row per DMR timeslot.
        b[0].buffer_type = MYSQL_TYPE_LONG;
        b[0].buffer = &rowId;
        // 1) mode
        Scratch s1(mode);
        b[1] = s1.bind_str();
        // 2) callsign
        Scratch s2(callsign);
        b[2] = s2.bind_str();
        // 3) dgid (nullable)
        NullableInt ni(dgid);
        b[3] = ni.bind_int();
        // 4) slot
        NullableInt nslot(slot);          
        b[4] = nslot.bind_int();
        // 5) source (nullable)
        Scratch s4(source.value_or(""));
        NullableStr ns4(source.has_value());
        b[5] = ns4.bind_str(s4);
        // 5) active (tinyint)
        my_bool active_flag = active ? 1 : 0;
        b[6].buffer_type = MYSQL_TYPE_TINY;
        b[6].buffer = &active_flag;
        // 6) ber (nullable double)
        NullableDouble nb(ber);
        b[7] = nb.bind_double();
        // 7) duration (nullable double)
        NullableDouble nd(duration);
        b[8] = nd.bind_double();
        // 8) updated_at = NOW() → kein Bind nötig (im SQL)

        if (mysql_stmt_bind_param(st_upsert_status, b) != 0) {
            dlog("[DB  ] bind upsert status failed: ", mysql_stmt_error(st_upsert_status));
            return;
        }
        if (mysql_stmt_execute(st_upsert_status) != 0) {
            dlog("[DB  ] exec upsert status failed: ", mysql_stmt_error(st_upsert_status));
        }
        if (mode == "DMR" && slot && (*slot == 1 || *slot == 2)) {
            rowId = *slot + 1;
            if (mysql_stmt_execute(st_upsert_status) != 0) {
                dlog("[DB  ] exec DMR slot status failed: ", mysql_stmt_error(st_upsert_status));
            }
        }
    }

void insertLastHeard(const std::string& callsign,
                     const std::string& mode,
                     const std::optional<int>& dgid,
                     const std::optional<int>& slot,
                     const std::optional<std::string>& source,
                     const std::optional<double>& duration,
                     const std::optional<double>& ber) {
        if (!ensure_conn() || !st_insert_lastheard) return;

        MYSQL_BIND b[7]{};
        Scratch s1(callsign); b[0] = s1.bind_str();
        Scratch s2(mode);     b[1] = s2.bind_str();
        NullableInt ni(dgid); b[2] = ni.bind_int();
        NullableInt nslot(slot);          b[3] = nslot.bind_int();
        Scratch s4(source.value_or(""));  NullableStr ns4(source.has_value()); b[4] = ns4.bind_str(s4);
        NullableDouble nd(duration);      b[5] = nd.bind_double();
        NullableDouble nb(ber);           b[6] = nb.bind_double();

        if (mysql_stmt_bind_param(st_insert_lastheard, b) != 0) {
            dlog("[DB  ] bind insert lastheard failed: ", mysql_stmt_error(st_insert_lastheard));
            return;
        }
        if (mysql_stmt_execute(st_insert_lastheard) != 0) {
            dlog("[DB  ] exec insert lastheard failed: ", mysql_stmt_error(st_insert_lastheard));
        }
    }

    void setReflectorDStar(const std::string& value) {
        upsertReflector(value, st_upsert_reflector_dstar, "dstar");
    }
    void setReflectorFusion(const std::string& value) {
        upsertReflector(value, st_upsert_reflector_fusion, "fusion");
    }
    void setReflectorDMR(const std::string& value) {
        upsertReflector(value, st_upsert_reflector_dmr, "dmr");
    }

    // Route aus ParsedResult
    void handleParsed(const ParsedResult& r) {
        // Start/Ende/Mode/Info
        if (r.startEnd == "Start") {
            upsertStatus(
                r.mode, r.callsign,
                r.dgId, r.slot,
                std::optional<std::string>(r.source.empty() ? "" : r.source),
                true, std::nullopt, std::nullopt
            );
        } else if (r.startEnd == "Ende") {
            // lastheard + status inactive
            insertLastHeard(
                r.callsign, r.mode, r.dgId,r.slot,
                std::optional<std::string>(r.source.empty() ? "" : r.source),
                r.durationSec, r.berPct
            );
            upsertStatus(
                r.mode, r.callsign, r.dgId,r.slot,
                std::optional<std::string>(r.source.empty() ? "" : r.source),
                false, r.berPct, r.durationSec
            );
        } else if (r.startEnd == "Mode") {
            if (r.mode == "Idle") {
                if (ensure_conn()) mysql_query(conn, "UPDATE status SET active=0 WHERE id IN (2,3)");
                // Nur Idle: Status auf inactive setzen und Felder leeren
                upsertStatus(
                    r.mode, "", std::nullopt,std::nullopt,
                    std::nullopt, false, std::nullopt, std::nullopt
                );
            } else {
                // Nicht-Idle Mode-Events (z. B. "Mode set to D-Star") NICHT in die DB schreiben,
                // damit ein kurz zuvor gesetzter Start-Status (active=1, Callsign=...) nicht überschrieben wird.
                // Konsolen-Print bleibt natürlich erhalten.
            }
        }
        else if (r.startEnd == "Info" && r.info.has_value()) {
            const std::string& msg = *r.info;
            if (r.mode == "YSF") {
                // Disconnect -> Reflector leeren
                if (msg == "DISCONNECTED" ||
                    msg.find("Disconnect by remote command") != std::string::npos ||
                    msg.find("Closing YSF network connection") != std::string::npos)
                {
                    // leerer String = „nicht verbunden“
                    setReflectorFusion("");
                } else {
                    // "Linked to <name>"
                    setReflectorFusion(stripPrefix(msg, "Linked to "));
                }
            } else if (r.mode == "D-Star") {
                // Slow data "Verlinkt zu <name>" oder beliebiger Text -> speichere voll
                setReflectorDStar(msg);
            } else if (r.mode == "DMR") {
                // "Logged into master: <server>"
                setReflectorDMR(stripPrefix(msg, "Logged into master: "));
            } else if (r.mode == "NXDN") {
                upsertReflector(msg, st_upsert_reflector_nxdn, "nxdn");
            }
        }
    }

private:
    // --- deine Konfig ---
    std::string host, user, pass, name;
    unsigned int port;
    std::string unix_socket;

    MYSQL* conn;
    bool initialStatus = true;
    MYSQL_STMT *st_upsert_status, *st_insert_lastheard;
    MYSQL_STMT *st_upsert_reflector_dstar, *st_upsert_reflector_fusion, *st_upsert_reflector_dmr;
    MYSQL_STMT *st_upsert_reflector_nxdn;

    // ---- Verbindungsaufbau + Statements (deine Snippets) ----
    bool connect() {
        if (conn) { mysql_close(conn); conn = nullptr; }
        conn = mysql_init(nullptr);
        if (!conn) { dlog("[DB  ] mysql_init failed"); return false; }
        unsigned int reconnect = 1;
        mysql_options(conn, MYSQL_OPT_RECONNECT, &reconnect);

        unsigned int proto = MYSQL_PROTOCOL_SOCKET;
        mysql_options(conn, MYSQL_OPT_PROTOCOL, &proto);

        if (!mysql_real_connect(
                conn,
                nullptr,                 // host: nullptr erzwingt lokalen Connect
                user.c_str(),            // "mmdvm"
                "",                      // KEIN Passwort
                name.c_str(),            // DB-Name
                0,                       // Port 0
                unix_socket.c_str(),     // Pfad zum Socket
                0)) {                    // Flags
            dlog("[DB  ] connect failed: ", mysql_error(conn));
            mysql_close(conn); conn = nullptr;
            return false;
        }

        dlog("[DB  ] connected via unix_socket=", unix_socket, " db=", name);
        const char* q1 =
            "CREATE TABLE IF NOT EXISTS lastheard ("
            " id INT AUTO_INCREMENT PRIMARY KEY,"
            " callsign VARCHAR(20),"
            " mode VARCHAR(20),"
            " dgid INT NULL,"
            " slot TINYINT NULL,"
            " source ENUM('RF','NET') NULL,"
            " duration FLOAT,"
            " ber FLOAT,"
            " ts DATETIME DEFAULT CURRENT_TIMESTAMP"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;";
        if (mysql_query(conn, q1) != 0) dlog("[DB  ] create lastheard failed: ", mysql_error(conn));

        const char* q2 =
            "CREATE TABLE IF NOT EXISTS status ("
            " id TINYINT PRIMARY KEY,"
            " mode VARCHAR(20),"
            " callsign VARCHAR(20),"
            " dgid INT NULL,"
            " slot TINYINT NULL,"
            " source ENUM('RF','NET') NULL,"
            " active BOOL,"
            " ber FLOAT,"
            " duration FLOAT,"
            " updated_at DATETIME"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;";
        if (mysql_query(conn, q2) != 0) dlog("[DB  ] create status failed: ", mysql_error(conn));

        const char* q3 =
            "CREATE TABLE IF NOT EXISTS reflector ("
            " id TINYINT PRIMARY KEY,"
            " dstar  VARCHAR(64) NULL,"
            " dmr    VARCHAR(64) NULL,"
            " fusion VARCHAR(64) NULL,"
            " updated_at DATETIME"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;";
        if (mysql_query(conn, q3) != 0) dlog("[DB  ] create reflector failed: ", mysql_error(conn));
        if (mysql_query(conn, "ALTER TABLE reflector ADD COLUMN IF NOT EXISTS nxdn VARCHAR(64) NULL") != 0)
            dlog("[DB  ] add NXDN reflector column failed: ", mysql_error(conn));

        mysql_query(conn, "INSERT IGNORE INTO reflector (id,dstar,dmr,fusion,updated_at) "
                          "VALUES (1,NULL,NULL,NULL,NOW());");

        // Remove the quoted D-Star names stored in Fusion by older parsers.
        mysql_query(conn, "UPDATE reflector SET fusion=NULL WHERE fusion REGEXP "
                          "'^(DCS|XRF|REF|XLX)[0-9]{3}[[:space:]]*[A-Z][[:space:]]*\"$'");

        prepare_statements();
        mysql_query(conn, "INSERT IGNORE INTO status (id,mode,callsign,dgid,slot,source,active,ber,duration,updated_at) "
                   "VALUES (1,'Idle','',NULL,NULL,'RF',0,NULL,NULL,NOW());");
        mysql_query(conn, "INSERT IGNORE INTO status (id,mode,callsign,slot,active,updated_at) "
                         "VALUES (2,'DMR','',1,0,NOW()), (3,'DMR','',2,0,NOW());");
        if (initialStatus) {
            mysql_query(conn, "UPDATE status SET active=0 WHERE active<>0");
            initialStatus = false;
        }
        return true;
    }

    void prepare_statements() {
        destroy_statements();

        const char* ps1 =
            "INSERT INTO status (id, mode, callsign, dgid, slot, source, active, ber, duration, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NOW()) "
            "ON DUPLICATE KEY UPDATE "
            " mode=VALUES(mode), callsign=VALUES(callsign), dgid=VALUES(dgid), slot=VALUES(slot), source=VALUES(source), "
            " active=VALUES(active), ber=VALUES(ber), duration=VALUES(duration), updated_at=NOW();";

        st_upsert_status = mysql_stmt_init(conn);
        if (!st_upsert_status || mysql_stmt_prepare(st_upsert_status, ps1, (unsigned long)strlen(ps1)) != 0) {
            dlog("[DB  ] prepare upsert status failed: ", mysql_error(conn));
            destroy_statements();
        }

        const char* ps2 =
            "INSERT INTO lastheard (callsign, mode, dgid, slot, source, duration, ber, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NOW());";

        st_insert_lastheard = mysql_stmt_init(conn);
        if (!st_insert_lastheard || mysql_stmt_prepare(st_insert_lastheard, ps2, (unsigned long)strlen(ps2)) != 0) {
            dlog("[DB  ] prepare insert lastheard failed: ", mysql_error(conn));
            destroy_statements();
        }

        const char* ps3 =
            "INSERT INTO reflector (id, dstar, updated_at) "
            "VALUES (1, ?, NOW()) "
            "ON DUPLICATE KEY UPDATE "
            " dstar=VALUES(dstar), updated_at=NOW();";

        st_upsert_reflector_dstar = mysql_stmt_init(conn);
        if (!st_upsert_reflector_dstar ||
            mysql_stmt_prepare(st_upsert_reflector_dstar, ps3, (unsigned long)strlen(ps3)) != 0) {
            dlog("[DB  ] prepare upsert reflector.dstar failed: ", mysql_error(conn));
            destroy_statements();
        }

        const char* ps4 =
            "INSERT INTO reflector (id, fusion, updated_at) "
            "VALUES (1, ?, NOW()) "
            "ON DUPLICATE KEY UPDATE "
            " fusion=VALUES(fusion), updated_at=NOW();";

        st_upsert_reflector_fusion = mysql_stmt_init(conn);
        if (!st_upsert_reflector_fusion ||
            mysql_stmt_prepare(st_upsert_reflector_fusion, ps4, (unsigned long)strlen(ps4)) != 0) {
            dlog("[DB  ] prepare upsert reflector.fusion failed: ", mysql_error(conn));
            destroy_statements();
        }

        const char* ps5 =
            "INSERT INTO reflector (id, dmr, updated_at) "
            "VALUES (1, ?, NOW()) "
            "ON DUPLICATE KEY UPDATE "
            " dmr=VALUES(dmr), updated_at=NOW();";

        st_upsert_reflector_dmr = mysql_stmt_init(conn);
        if (!st_upsert_reflector_dmr ||
            mysql_stmt_prepare(st_upsert_reflector_dmr, ps5, (unsigned long)strlen(ps5)) != 0) {
            dlog("[DB  ] prepare upsert reflector.dmr failed: ", mysql_error(conn));
            destroy_statements();
        }
        const char* ps6 = "INSERT INTO reflector (id,nxdn,updated_at) VALUES (1,?,NOW()) "
                          "ON DUPLICATE KEY UPDATE nxdn=VALUES(nxdn),updated_at=NOW()";
        st_upsert_reflector_nxdn = mysql_stmt_init(conn);
        if (!st_upsert_reflector_nxdn || mysql_stmt_prepare(st_upsert_reflector_nxdn, ps6, strlen(ps6)) != 0) {
            dlog("[DB  ] prepare NXDN reflector failed: ", mysql_error(conn));
            destroy_statements();
        }
    }

    void destroy_statements() {
        if (st_upsert_status) { mysql_stmt_close(st_upsert_status); st_upsert_status = nullptr; }
        if (st_insert_lastheard) { mysql_stmt_close(st_insert_lastheard); st_insert_lastheard = nullptr; }
        if (st_upsert_reflector_dstar) { mysql_stmt_close(st_upsert_reflector_dstar); st_upsert_reflector_dstar = nullptr; }
        if (st_upsert_reflector_fusion) { mysql_stmt_close(st_upsert_reflector_fusion); st_upsert_reflector_fusion = nullptr; }
        if (st_upsert_reflector_dmr) { mysql_stmt_close(st_upsert_reflector_dmr); st_upsert_reflector_dmr = nullptr; }
        if (st_upsert_reflector_nxdn) { mysql_stmt_close(st_upsert_reflector_nxdn); st_upsert_reflector_nxdn = nullptr; }
    }

    // ---- Helpers für Binds ----
    struct Scratch {
        std::vector<char> buf;
        explicit Scratch(const std::string& s) : buf(s.begin(), s.end()) { buf.push_back('\0'); }
        MYSQL_BIND bind_str() {
            MYSQL_BIND b{};
            b.buffer_type = MYSQL_TYPE_STRING;
            b.buffer = buf.data();
            unsigned long* len = new unsigned long; // wird von lib nicht freigegeben; wir nutzen nullterm, also:
            *len = (unsigned long)(buf.size() - 1);
            b.length = len; // safe, da Lebenszeit > execute; (minimaler Leck – ok hier, da 3 Stmts, selten aufgerufen)
            return b;
        }
    };
    struct NullableStr {
        my_bool is_null;
        explicit NullableStr(bool has) : is_null(has ? 0 : 1) {}
        MYSQL_BIND bind_str(Scratch& s) {
            MYSQL_BIND b{};
            b.buffer_type = MYSQL_TYPE_STRING;
            b.buffer = s.buf.data();
            unsigned long* len = new unsigned long;
            *len = is_null ? 0 : (unsigned long)(s.buf.size() - 1);
            b.length = len;
            b.is_null = &is_null;
            return b;
        }
    };
    struct NullableInt {
        long long v; my_bool is_null;
        explicit NullableInt(const std::optional<int>& o)
            : v(o.has_value()? *o : 0), is_null(o.has_value()? 0:1) {}
        MYSQL_BIND bind_int() {
            MYSQL_BIND b{};
            b.buffer_type = MYSQL_TYPE_LONGLONG;
            b.buffer = &v;
            b.is_null = &is_null;
            return b;
        }
    };
    struct NullableDouble {
        double v; my_bool is_null;
        explicit NullableDouble(const std::optional<double>& o)
            : v(o.has_value()? *o : 0.0), is_null(o.has_value()? 0:1) {}
        MYSQL_BIND bind_double() {
            MYSQL_BIND b{};
            b.buffer_type = MYSQL_TYPE_DOUBLE;
            b.buffer = &v;
            b.is_null = &is_null;
            return b;
        }
    };

    void upsertReflector(const std::string& value, MYSQL_STMT* stmt, const char* which) {
        if (!ensure_conn() || !stmt) return;
        Scratch s(value);
        MYSQL_BIND b[1]{};
        b[0] = s.bind_str();
        if (mysql_stmt_bind_param(stmt, b) != 0) {
            dlog("[DB  ] bind upsert reflector.", which, " failed: ", mysql_stmt_error(stmt));
            return;
        }
        if (mysql_stmt_execute(stmt) != 0) {
            dlog("[DB  ] exec upsert reflector.", which, " failed: ", mysql_stmt_error(stmt));
        }
    }

    static std::string stripPrefix(const std::string& s, const char* pfx) {
        size_t n = std::strlen(pfx);
        if (s.size() >= n && s.compare(0, n, pfx) == 0) return s.substr(n);
        return s;
    }
};

static uint64_t processFileFromOffset(const std::string& path,
                                      LogParser& parser,
                                      Database& db,
                                      uint64_t startOffset, bool separateBoard = false)
{
    std::ifstream f(path, std::ios::in | std::ios::binary);
    if (!f) {
        static std::unordered_set<std::string> warned; // <unordered_set> includen
        if (!warned.count(path)) {
            std::cerr << "[warn] kann Datei nicht öffnen: " << path << " (evtl. Rechte?)\n";
            warned.insert(path);
        }
        return startOffset;
    }


    // zum Startoffset springen (falls größer als aktuelle Größe, fangen wir bei 0 an)
    f.seekg(0, std::ios::end);
    auto sz = static_cast<uint64_t>(f.tellg());
    if (startOffset > sz) startOffset = 0;
    f.clear();
    f.seekg(static_cast<std::streamoff>(startOffset), std::ios::beg);

    std::string line, lastLine;
    std::streampos lastGoodPos = f.tellg();
    while (true) {
        std::streampos before = f.tellg();
        if (!std::getline(f, line)) break;

        // Nach erfolgreichem getline() zeigt tellg() auf die Position NACH dem '\n'
        std::streampos after = f.tellg();
        if (after != std::streampos(-1)) {
            lastGoodPos = after;  // nur vollständige Zeilen (mit '\n') vorziehen
        }
        // Strip trailing '\r' (falls CRLF)
        if (!line.empty() && line.back() == '\r') line.pop_back();

        lastLine = line;

        auto res = parser.processLine(line, std::filesystem::path(path).filename() == "NXDNGateway.log");

        // zuerst evtl. erzwungene Enden
        for (const auto& p : parser.takePending()) {
            printResult(p);
            db.handleParsed(p);
        }

        // dann das aktuelle Ergebnis
        if (res && !(separateBoard && res->startEnd == "Mode" && res->mode == "Idle")) {
            printResult(*res);
            db.handleParsed(*res);
        }
    }

    // neuen Offset setzen:
    // - Wenn wir mindestens eine vollständige Zeile gelesen haben: lastGoodPos
    // - Wenn gar nichts gelesen wurde: startOffset (nicht vorspulen!)
    if (lastGoodPos == std::streampos(-1)) {
        // Sicherheitsnetz (sollte eigentlich nicht passieren)
        return startOffset;
    }
    uint64_t newOffset = static_cast<uint64_t>(lastGoodPos);

    return newOffset;
}

// Liest die Datei komplett von 0..EOF, verarbeitet NUR "Info"-Events (Reflector/Master/Slowdata),
// ignoriert alles andere. Keine Konsolen-Ausgabe, nur DB-Upserts.
static void backfillReflectorsFromFile(const std::string& path, Database& db) {
    std::ifstream f(path, std::ios::in | std::ios::binary);
    if (!f) return;

    LogParser tmp; // eigener, kurzlebiger Parser (keine Wechselwirkung mit dem Live-Parser)
    std::string line;
    while (std::getline(f, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (auto res = tmp.processLine(line, std::filesystem::path(path).filename() == "NXDNGateway.log")) {
            // Pending (erzwungene Enden) verwerfen
            tmp.takePending();
            if (res->startEnd == "Info") {
                db.handleParsed(*res); // schreibt reflector.{fusion,dmr,dstar}
            }
        }
    }
}

int main(int argc, char** argv) {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);

    std::cout.setf(std::ios::unitbuf);
    std::cerr.setf(std::ios::unitbuf);

    // Parser bleibt über die gesamte Laufzeit bestehen
    LocalConfig cfg = readLocalConfig();
    LogParser parser(cfg);
    std::map<std::string, LogParser> boardParsers;
    std::map<std::string, std::filesystem::file_time_type> boardConfigTimes;
    Database   db;

    // Argumente gemerkt: wenn keine angegeben sind, beobachten wir immer die heutigen Standardpfade
    std::vector<std::string> argPaths;
    if (argc > 1) {
        for (int i = 1; i < argc; ++i) argPaths.emplace_back(argv[i]);
    }

    auto offsets = loadOffsets();

    bool did_backfill = false;

    // Endlosschleife: tail -F
    for (;;) {
        std::vector<std::string> paths;
        if (argPaths.empty()) {
            paths = defaultLogPaths();
        } else {
            // Argumente expandieren: Verzeichnisse -> 3 Log-Dateien; Dateien -> unverändert
            for (const auto& ap : argPaths) {
                if (std::filesystem::is_directory(ap)) {
                    auto v = logsForDir(ap);
                    paths.insert(paths.end(), v.begin(), v.end());
                } else {
                    paths.push_back(ap);
                }
            }
        }

        // Einmaliger Backfill nur für Link-Infos ----
        if (!did_backfill) {
            for (const auto& p : paths) {

                auto st = statFile(p);
                if (!st) {
                    continue;
                }
                backfillReflectorsFromFile(p, db);
                // setze Offset auf EOF, damit wir gleich im Tail-Modus weitermachen
                offsets[p] = OffsetEntry{st->inode, static_cast<uint64_t>(st->size)};
            }
            saveOffsets(offsets);
            did_backfill = true;
        }

        bool anyProcessed = false;
        std::map<std::string, uint64_t> lastReadCounts;

        for (const auto& p : paths) {
            auto st = statFile(p);
            if (!st) {
                continue;
            }
            std::smatch board;
            const std::string filename = std::filesystem::path(p).filename().string();
            const bool separateBoard = std::regex_match(filename, board,
                std::regex(R"(^MMDVM-board-([a-f0-9]{12})-\d{4}-\d{2}-\d{2}\.log$)"));

            uint64_t lastInode = 0, lastOffset = 0;
            if (auto it = offsets.find(p); it != offsets.end()) {
                lastInode  = it->second.inode;
                lastOffset = it->second.offset;
            }

            if (st->inode != lastInode || static_cast<uint64_t>(st->size) < lastOffset) {
                lastOffset = 0;
            }

            // Wenn neu beobachtet: am Ende beginnen
            if (!separateBoard && offsets.find(p) == offsets.end()) {
                lastOffset = static_cast<uint64_t>(st->size);
            }

            uint64_t before = lastOffset;
            LogParser* sourceParser = &parser;
            if (separateBoard) {
                const std::string id = board[1];
                const std::string config = "/run/opendvm-host/" + id + ".ini";
                std::error_code error;
                const auto modified = std::filesystem::last_write_time(config, error);
                if (!error && boardConfigTimes[id] != modified) {
                    boardParsers.erase(id);
                    boardConfigTimes[id] = modified;
                }
                auto found = boardParsers.find(id);
                if (found == boardParsers.end())
                    found = boardParsers.try_emplace(id, readLocalConfig(config)).first;
                sourceParser = &found->second;
            }
            uint64_t newOffset = processFileFromOffset(p, *sourceParser, db, lastOffset, separateBoard);
            uint64_t delta = (newOffset >= before) ? (newOffset - before) : 0;
            uint64_t approxLines = delta / 120; // grobe Annahme (durchschnittlich 120 Bytes pro Zeile)
            lastReadCounts[p] = approxLines;

            offsets[p] = OffsetEntry{st->inode, newOffset};
            anyProcessed = true;
        }

        if (anyProcessed) {
            saveOffsets(offsets);
            //printStatus(offsets, lastReadCounts);
        }

        std::this_thread::sleep_for(std::chrono::seconds(1));
    }


    return 0;
}
