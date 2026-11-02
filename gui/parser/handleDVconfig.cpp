#include <iostream>
#include <string>
#include <iomanip>
#include <sstream>
#include <cstdio>
#include <array>
#include <nlohmann/json.hpp>
#include "handleDVconfig.h"
#include "helper.h"

handleDVconfig::handleDVconfig()
    : host_renderer("/etc/MMDVMHost.ini", true)
    , ircddb_renderer("/etc/ircddbgateway", true)
    , dmr_renderer("/etc/dmrgateway", true)
    , ysf_renderer("/etc/ysfgateway", true)
{
}

/** Populate 'site' from the various renderers. */
void handleDVconfig::readConfig() {
    // host -> site
    site.Callsign    = host_renderer.findValue("General", "Callsign");
    site.Id          = host_renderer.findValue("General", "Id");
    site.Duplex      = host_renderer.findValue("General", "Duplex");
    site.RXFrequency = host_renderer.findValue("Info",    "RXFrequency");
    site.TXFrequency = host_renderer.findValue("Info",    "TXFrequency");
    site.Longitude   = host_renderer.findValue("Info",    "Longitude");
    site.Latitude    = host_renderer.findValue("Info",    "Latitude");
    site.Height      = host_renderer.findValue("Info",    "Height");
    site.Location    = host_renderer.findValue("Info",    "Location");
    site.Description = host_renderer.findValue("Info",    "Description");
    site.URL         = host_renderer.findValue("Info",    "URL");
    site.Module      = host_renderer.findValue("D-Star",  "Module");

    // ircddb (flat)
    site.reflector1  = ircddb_renderer.findValue("", "reflector1");

    // ysf
    site.Suffix      = ysf_renderer.findValue("General", "Suffix");
    site.Startup     = ysf_renderer.findValue("Network", "Startup");
    site.Options     = ysf_renderer.findValue("Network", "Options");

    // dmr
    site.Address     = dmr_renderer.findValue("DMR Network 1", "Address");
    site.Password    = dmr_renderer.findValue("DMR Network 1", "Password");
    site.Name        = dmr_renderer.findValue("DMR Network 1", "Name");
    if (FILE* pipe = popen("sudo -n /usr/local/sbin/opendvm-mode-control boot-status", "r")) {
        std::string output;
        std::array<char, 1024> buffer{};
        while (fgets(buffer.data(), buffer.size(), pipe)) output += buffer.data();
        int result = pclose(pipe);
        try {
            auto data = nlohmann::json::parse(output);
            if (result == 0) {
                site.DstarStartOnBoot = data.at("dstar").get<bool>();
                site.DmrStartOnBoot = data.at("dmr").get<bool>();
                site.YsfStartOnBoot = data.at("ysf").get<bool>();
            }
        } catch (const std::exception& error) {
            std::fprintf(stderr, "[modes] cannot read boot settings: %s\n", error.what());
        }
    }
}

// schreibt 'site' in die 4 Config-Dateien
bool handleDVconfig::saveConfig() {
    applyError.clear();
    // Runtime controls may have changed Enable flags since this daemon started.
    host_renderer = renderConfigFile("/etc/MMDVMHost.ini", false);
    ircddb_renderer = renderConfigFile("/etc/ircddbgateway", false);
    dmr_renderer = renderConfigFile("/etc/dmrgateway", false);
    ysf_renderer = renderConfigFile("/etc/ysfgateway", false);
    // 1) Fatal: alle Dateien müssen existieren/lesbar sein
    if (!host_renderer.isLoaded())   { std::fprintf(stderr, "[saveConfig] missing/unreadable: /etc/MMDVMHost.ini\n"); return false; }
    if (!ircddb_renderer.isLoaded()) { std::fprintf(stderr, "[saveConfig] missing/unreadable: /etc/ircddbgateway\n"); return false; }
    if (!ysf_renderer.isLoaded())    { std::fprintf(stderr, "[saveConfig] missing/unreadable: /etc/ysfgateway\n"); return false; }
    if (!dmr_renderer.isLoaded())    { std::fprintf(stderr, "[saveConfig] missing/unreadable: /etc/dmrgateway\n"); return false; }

    // 2) Werte setzen
    bool chg_host, chg_ircddb, chg_ysf, chg_dmr;

    // /etc/MMDVMHost.ini
    chg_host =  host_renderer.setValue("Callsign",    site.Callsign,   "General");
    chg_host |= host_renderer.setValue("Id",          site.Id,         "General");
    chg_host |= host_renderer.setValue("Duplex",      site.Duplex,     "General");
    chg_host |= host_renderer.setValue("RXFrequency", site.RXFrequency, "Info");
    chg_host |= host_renderer.setValue("TXFrequency", site.TXFrequency, "Info");
    chg_host |= host_renderer.setValue("Longitude",   site.Longitude,   "Info");
    chg_host |= host_renderer.setValue("Latitude",    site.Latitude,    "Info");
    chg_host |= host_renderer.setValue("Height",      site.Height,      "Info");
    chg_host |= host_renderer.setValue("Location",    site.Location,    "Info");
    chg_host |= host_renderer.setValue("Description", site.Description, "Info");
    chg_host |= host_renderer.setValue("URL",         site.URL,         "Info");
    chg_host |= host_renderer.setValue("Module",      site.Module,     "D-Star");

    // /etc/ircddbgateway (flat)
    double tx = std::stoll(site.TXFrequency) / 1e6;
    double rx = std::stoll(site.RXFrequency) / 1e6;
    double off = tx - rx;
    std::ostringstream rxs, offs;
    rxs << std::fixed << std::setprecision(6) << rx;
    offs << std::fixed << std::setprecision(6) << off;

    chg_ircddb = ircddb_renderer.setValue("gatewayCallsign", site.Callsign     , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("latitude",        site.Latitude     , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("longitude",       site.Longitude    , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("description1",    site.Location     , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("description2",    site.Description  , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("url",             site.URL          , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("repeaterCall1",   site.Callsign     , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("repeaterBand1",   site.Module       , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("frequency1",      rxs.str()         , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("offset1",         offs.str()        , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("latitude1",       site.Latitude     , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("longitude1",      site.Longitude    , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("description1_1",  site.Location     , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("description1_2",  site.Description  , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("url1",            site.URL          , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("ircddbUsername",  site.Callsign     , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("dplusLogin",      site.Callsign     , /*section*/"");
    chg_ircddb |= ircddb_renderer.setValue("reflector1",      site.reflector1   , /*section*/"");

    // /etc/ysfgateway
    chg_ysf = ysf_renderer.setValue("Callsign",       site.Callsign,  "General");
    chg_ysf |= ysf_renderer.setValue("Suffix",         site.Suffix,    "General");
    chg_ysf |= ysf_renderer.setValue("Id",             site.Id,        "General");
    chg_ysf |= ysf_renderer.setValue("RXFrequency",    site.RXFrequency, "Info");
    chg_ysf |= ysf_renderer.setValue("TXFrequency",    site.TXFrequency, "Info");
    chg_ysf |= ysf_renderer.setValue("Latitude",       site.Latitude,  "Info");
    chg_ysf |= ysf_renderer.setValue("Longitude",      site.Longitude, "Info");
    chg_ysf |= ysf_renderer.setValue("Height",         site.Height,    "Info");
    chg_ysf |= ysf_renderer.setValue("Name",           site.Name,      "Info");
    chg_ysf |= ysf_renderer.setValue("Description",    site.Description,"Info");
    chg_ysf |= ysf_renderer.setValue("Startup",        site.Startup,   "Network");
    chg_ysf |= ysf_renderer.setValue("Options",        site.Options,   "Network");

    // /etc/dmrgateway
    chg_dmr = dmr_renderer.setValue("RXFrequency",site.RXFrequency,   "Info");
    chg_dmr |= dmr_renderer.setValue("TXFrequency",site.TXFrequency,   "Info");
    chg_dmr |= dmr_renderer.setValue("Latitude",   site.Latitude,      "Info");
    chg_dmr |= dmr_renderer.setValue("Longitude",  site.Longitude,     "Info");
    chg_dmr |= dmr_renderer.setValue("Height",     site.Height,        "Info");
    chg_dmr |= dmr_renderer.setValue("Location",   site.Location,      "Info");
    chg_dmr |= dmr_renderer.setValue("Description",site.Description,   "Info");
    chg_dmr |= dmr_renderer.setValue("URL",        site.URL,           "Info");

    chg_dmr |= dmr_renderer.setValue("Address",    site.Address,       "DMR Network 1");
    chg_dmr |= dmr_renderer.setValue("Password",   site.Password,      "DMR Network 1");
    chg_dmr |= dmr_renderer.setValue("Name",       site.Name,          "DMR Network 1");
    // Default ist enable=0
    chg_dmr |= dmr_renderer.setValue("Enabled",     "1",                "DMR Network 1");
    
    chg_dmr |= dmr_renderer.setValue("Id",     site.Id,     "XLX Network");
    chg_dmr |= dmr_renderer.setValue("Id",     site.Id,     "DMR Network 1");
    chg_dmr |= dmr_renderer.setValue("Id",     site.Id,     "DMR Network 2");
    chg_dmr |= dmr_renderer.setValue("Id",     site.Id,     "DMR Network 5");
    chg_dmr |= dmr_renderer.setValue("Id",     site.Id,     "DMR Network Custom");

    // 3) Persist only if changed; restart corresponding service after successful save
    if (chg_host) {
        printf("save mmdvmhost config data and restart service\n");
        if (!host_renderer.saveConfigFile()) { std::fprintf(stderr, "[saveConfig] write failed: /etc/MMDVMHost.ini\n"); return false; }
    }
    if (chg_ircddb) {
        printf("save ircddb config data and restart service\n");
        if (!ircddb_renderer.saveConfigFile()) { std::fprintf(stderr, "[saveConfig] write failed: /etc/ircddbgateway\n"); return false; }
    }
    if (chg_ysf) {
        printf("save ysf config data and restart service\n");
        if (!ysf_renderer.saveConfigFile()) { std::fprintf(stderr, "[saveConfig] write failed: /etc/ysfgateway\n"); return false; }
    }
    if (chg_dmr) {
        printf("save dmr config data and restart service\n");
        if (!dmr_renderer.saveConfigFile()) { std::fprintf(stderr, "[saveConfig] write failed: /etc/dmrgateway\n"); return false; }
    }

    // Apply RF flags, gateway state and boot policy together, after all files
    // have been written. The controller queues one shared-modem restart.
    const std::string command = std::string("sudo -n /usr/local/sbin/opendvm-mode-control configure ")
        + (site.DstarStartOnBoot ? "1 " : "0 ")
        + (site.DmrStartOnBoot ? "1 " : "0 ")
        + (site.YsfStartOnBoot ? "1" : "0");
    FILE* pipe = popen(command.c_str(), "r");
    if (!pipe) { applyError = "Mode controller could not start"; return false; }
    std::string output;
    std::array<char, 1024> buffer{};
    while (fgets(buffer.data(), buffer.size(), pipe)) output += buffer.data();
    int result = pclose(pipe);
    if (result != 0) {
        try { applyError = nlohmann::json::parse(output).value("error", "Service changes failed"); }
        catch (...) { applyError = "Service changes failed"; }
    }
    return result == 0;
}
