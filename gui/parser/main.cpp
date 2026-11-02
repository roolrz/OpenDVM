#include <thread>
#include <chrono>
#include <csignal>
#include <atomic>
#include "handleDVconfig.h"
#include "Database.h"
#include "MqttListener.h"

static std::atomic<bool> g_running{true};

void sigHandler(int)
{
    g_running = false;
    MqttListener::stop();
}

int main(){
    // Nach dem Programmstart fülle die Datenbank einmalig
    handleDVconfig dv;
    dv.readConfig();           // fills dv.site
    Database db;
    db.completeApply(false, "Configuration service restarted during apply; review settings and save again");
    db.writeSiteData(dv.site); // push to DB (id=1)

    // Starte FM Funknetz Abfragen als Thread
    MqttListener::init();
    std::signal(SIGINT,  sigHandler);
    std::signal(SIGTERM, sigHandler);
    MqttListener::start();

    // führe DV Logfile Parsing in dieser Loop aus
    while(g_running) {
        bool newdata = db.readSiteData(dv.site);
        if(newdata) {
            // handle data
            printf("new data from GUI\n");
            try {
                bool ok = dv.saveConfig();
                db.completeApply(ok, ok ? "" : (dv.applyError.empty() ? "Writing gateway configuration failed" : dv.applyError));
            } catch (const std::exception& error) {
                db.completeApply(false, error.what());
            }
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    return 0;
}
