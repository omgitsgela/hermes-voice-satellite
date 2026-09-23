/**
 *  Hermes Announcer
 *
 *  Virtual device that makes Hermes speak out loud on the voice satellite in the house.
 *
 *  Call speak() or announce() with a message from Rule Machine, an automation, or the device
 *  page, and Hermes announces it on the satellite speaker: a wake tone, a one second beat, then
 *  the message read verbatim.
 *
 *  Round trip: this driver -> POST http://<autumn>:8765/announce -> the Hermes voice bridge ->
 *  the satellite speaker. Nothing leaves the house and no language model is involved, so what
 *  you type is exactly what is spoken. Reserved for important notifications by design: the
 *  bridge drops repeats of the same message within 60 seconds and refuses anything over 400
 *  characters.
 *
 *  The deliverable is "accepted", not "spoken". The bridge queues the message and speaks it once
 *  any conversation in progress has finished; a drop (satellite unreachable) is recorded in
 *  /home/angela/hermes-voice-bridge/logs/announcements.jsonl on autumn and is not retried.
 *
 *  2026-09-22  1.0.0  Angela  Initial release.
 */

metadata {
    definition(
        name: "Hermes Announcer",
        namespace: "custom",
        author: "Angela",
        description: "Speaks a message out loud on the Hermes voice satellite.",
        category: "Convenience"
    ) {
        capability "Actuator"
        // Speech Synthesis is what makes this device selectable by Rule Machine's "Speak this
        // message" action; that action maps onto speak(String) below.
        capability "Speech Synthesis"

        command "announce", [[name: "Message*", type: "STRING",
                              description: "Text to announce, spoken verbatim"]]
        command "testAnnounce"

        attribute "lastAnnouncement", "STRING"
        attribute "lastAnnounceResult", "STRING"
    }

    preferences {
        input name: "announceUrl", type: "text", title: "Announce endpoint URL",
            defaultValue: "http://192.168.0.142:8765/announce", required: true
        input name: "apiToken", type: "password", title: "API token",
            description: "Copy it from /home/angela/hermes-voice-bridge/state/announce-token.txt on autumn",
            required: true
        input name: "sourceName", type: "text", title: "Source label",
            description: "Recorded in the announcement log on autumn", defaultValue: "hubitat",
            required: false
        input name: "useTone", type: "bool", title: "Start with the wake tone",
            defaultValue: true
        input name: "txtEnable", type: "bool", title: "Enable description text logging?",
            defaultValue: true
        input name: "debugEnable", type: "bool", title: "Enable debug logging?",
            defaultValue: false
    }
}

static String version() { "1.0.0" }

/* ============ Lifecycle ============ */

void installed() {
    logInfo "installed() v${version()}"
    initialize()
}

void updated() {
    logInfo "updated() v${version()}"
    initialize()
}

void initialize() {
    sendEvent(name: "lastAnnounceResult", value: "ready", isStateChange: true, type: "digital")
}

/* ============ Speaking ============ */

/** Rule Machine's speak action lands here. */
void speak(String message) {
    announce(message)
}

void announce(String message) {
    String text = (message ?: "").toString().trim()
    if (!text) {
        logWarn "announce() called with no message; nothing was sent"
        result("empty message")
        return
    }
    if (!announceUrl || !apiToken) {
        logWarn "announce() called before the endpoint URL and token were set"
        result("not configured")
        return
    }

    // Trim the token. Copying it out of a file or a clipboard can carry a trailing newline, and a
    // header value containing one fails authorisation in a way that looks exactly like a wrong key.
    String token = (apiToken ?: "").toString().trim()
    Map params = [
        uri: announceUrl,
        requestContentType: "application/json",
        contentType: "application/json",
        headers: ["Authorization": "Bearer ${token}"],
        body: [text: text, source: (sourceName ?: "hubitat"), chime: (useTone != false)],
        timeout: 15,
        ignoreSSLIssues: true
    ]

    logDebug "POST ${announceUrl} (${text.length()} chars)"
    try {
        asynchttpPost("announceResponse", params)
        // The bridge answers as soon as the text is queued, so this is a send confirmation.
        sendEvent(name: "lastAnnouncement", value: text, isStateChange: true, type: "digital",
                  descriptionText: "Announcement sent: ${text}")
        result("sent")
        logInfo "announcement sent: ${text}"
    } catch (Exception e) {
        logWarn "announce() could not send: ${e}"
        result("send failed")
    }
}

void announceResponse(response, data) {
    if (response == null) {
        logWarn "announcement response was empty"
        result("no response")
        return
    }
    Integer status = response.getStatus()
    if (status == 202 || status == 200) {
        if (response.getJson()?.duplicate) {
            logInfo "the bridge ignored this as a duplicate of a recent announcement"
            result("duplicate ignored")
        } else {
            logInfo "announcement accepted (HTTP ${status})"
            result("accepted")
        }
    } else if (status == 401) {
        logWarn "the bridge rejected the token (HTTP 401); check the API token preference"
        result("token rejected")
    } else if (status == 400 || status == 503) {
        logWarn "the bridge refused the announcement (HTTP ${status}): ${response.getData()}"
        result("refused (HTTP ${status})")
    } else {
        logWarn "unexpected answer from the bridge (HTTP ${status}): ${response.getData()}"
        result("error (HTTP ${status})")
    }
}

void testAnnounce() {
    announce("This is a test announcement from Hubitat.")
}

/* ============ Helpers ============ */

private void result(String value) {
    sendEvent(name: "lastAnnounceResult", value: value, isStateChange: true, type: "digital")
}

private void logDebug(msg) {
    if (debugEnable) log.debug msg
}

private void logInfo(msg) {
    if (txtEnable) log.info msg
}

private void logWarn(msg) {
    log.warn msg
}
