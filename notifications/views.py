# SPDX-License-Identifier: GPL-3.0-or-later
"""The in-app half of doc 09: the list, the badge, the subscription.

The list exists so the Push never has to be the only copy (§5). A notification
refused by the browser, muted, or held outside the delivery window is still
here -- which is what makes the channel repairable.
"""

import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from accounts.models import PushSubscription

from .models import Kind, Notification


@login_required
def notification_list(request):
    notifications = (
        Notification.objects.filter(recipient=request.user)
        .select_related("ticket", "actor")[:100]
    )
    return render(request, "notifications/list.html", {
        "notifications": notifications,
        # unread_count comes from the context processor, and is read here to
        # decide whether "mark all read" is worth drawing.
        "vapid_public_key": settings.ST_VAPID_PUBLIC_KEY,
        "has_device": PushSubscription.objects.filter(user=request.user).exists(),
    })


@require_POST
@login_required
def mark_read(request):
    """Everything at once. Per-notification read state would be a feature
    nobody asked for: the list is short and the badge is the point."""
    Notification.objects.filter(recipient=request.user, read_at__isnull=True).update(
        read_at=timezone.now()
    )
    return redirect("notifications:list")


@login_required
def open_notification(request, pk):
    """Follow one through to what it is about, marking it read on the way."""
    try:
        notification = Notification.objects.get(pk=pk, recipient=request.user)
    except Notification.DoesNotExist:
        raise Http404
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at"])
    if notification.ticket_id:
        # No visibility check needed here: the ticket view does it, and does it
        # the one way (D-21). A ticket that became invisible answers 404.
        return redirect("tickets:detail", pk=notification.ticket_id)
    return redirect("/admin/parc/syncdecision/")


@ensure_csrf_cookie
@login_required
def subscribe(request):
    """Register one device (D-14).

    The endpoint and its two keys come from the browser's push service; we
    store them and nothing else. Idempotent on the endpoint, because a browser
    hands back the same one every time it is asked.
    """
    if request.method != "POST":
        raise Http404
    try:
        payload = json.loads(request.body)
        endpoint = payload["endpoint"]
        keys = payload["keys"]
        p256dh, auth = keys["p256dh"], keys["auth"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"error": "malformed subscription"}, status=400)

    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            "user": request.user,
            "p256dh": p256dh,
            "auth": auth,
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:255],
            "last_used_at": timezone.now(),
        },
    )
    return JsonResponse({"ok": True})


@require_POST
@login_required
def unsubscribe(request):
    endpoint = request.POST.get("endpoint") or json.loads(request.body or "{}").get("endpoint")
    query = PushSubscription.objects.filter(user=request.user)
    if endpoint:
        query = query.filter(endpoint=endpoint)
    query.delete()
    return JsonResponse({"ok": True})


def service_worker(request):
    """Served from the site root on purpose.

    A service worker may only control pages **below its own URL**. Served from
    /static/sw.js it would control /static/ and nothing else, and no push would
    ever be displayed. This is the single reason this view exists.
    """
    return render(request, "notifications/sw.js", content_type="application/javascript")
