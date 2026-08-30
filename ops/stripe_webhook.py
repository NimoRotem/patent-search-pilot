#!/usr/bin/env python
"""Register (or inspect) this app's Stripe webhook endpoint and print its signing secret.

    ops/stripe_webhook.py --list
    ops/stripe_webhook.py --create        # prints STRIPE_WEBHOOK_SECRET for the .env
    ops/stripe_webhook.py --delete we_xxx

THE SECRET IS SHOWN ONCE, at creation. Stripe will not hand it back afterwards, so if it is lost
the endpoint has to be deleted and made again.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import billing                                                            # noqa: E402

URL = os.environ.get("STRIPE_WEBHOOK_URL", "https://nimo.iptorch.com/billing/webhook")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--delete", default="")
    a = ap.parse_args()
    s = billing._stripe()

    if a.delete:
        s.WebhookEndpoint.delete(a.delete)
        print("deleted", a.delete)
        return
    if a.create:
        ep = s.WebhookEndpoint.create(url=URL, enabled_events=list(billing.WEBHOOK_EVENTS),
                                      description="IPtorch usage billing")
        print("created %s -> %s" % (ep.id, ep.url))
        print("events: %s" % ", ".join(ep.enabled_events))
        print()
        print("STRIPE_WEBHOOK_SECRET=%s" % ep.secret)
        return
    for ep in s.WebhookEndpoint.list(limit=20).auto_paging_iter():
        print("%-24s %-6s %s" % (ep.id, ep.status, ep.url))
        print("    %s" % ", ".join(ep.enabled_events))


if __name__ == "__main__":
    main()
