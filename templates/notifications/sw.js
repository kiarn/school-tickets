/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Service worker: displays a Push and opens what it is about. Nothing else --
 * no caching, no offline shell. See specs/09-notifications.md.
 *
 * Served from the site root by notifications.views.service_worker, because a
 * worker only controls pages below its own URL.
 */

self.addEventListener("push", function (event) {
    /* The payload is deliberately poor (doc 09 §2): kind and room, never a
       description, never a name. It is read off a locked screen. */
    var data = {};
    try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }

    event.waitUntil(self.registration.showNotification(
        data.title || "school-tickets",
        {
            body: data.body || "",
            tag: data.url || "school-tickets",
            /* Replaces rather than stacks: three notifications about the same
               ticket are one line, not three. */
            renotify: false,
            data: { url: data.url || "/" }
        }
    ));
});

self.addEventListener("notificationclick", function (event) {
    event.notification.close();
    var url = (event.notification.data && event.notification.data.url) || "/";

    /* Focus a tab already on the site rather than opening a fourth one. */
    event.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true })
        .then(function (windows) {
            for (var i = 0; i < windows.length; i++) {
                if ("focus" in windows[i]) {
                    windows[i].navigate(url);
                    return windows[i].focus();
                }
            }
            return clients.openWindow(url);
        }));
});
