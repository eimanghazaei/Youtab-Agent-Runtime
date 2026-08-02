---
title: "AI Processing"
sidebar_label: "AI Processing"
sidebar_position: 1
---

# AI Processing

Youtab runs on **Alpha v0.6**, Youtab's managed reasoning agent. There is
nothing to configure: you sign in to Youtab and Alpha is there.

## What you need to do

Nothing. Alpha v0.6 is provisioned, operated and paid for by Youtab. You do not
supply an API key, choose an engine, or manage a model. Signing in is the whole
setup.

Select the agent you want from the **Agent** selector in Chat, or open the
**Agents** app to see every agent available to your account.

## Bringing your own API key

Youtab does not offer bring-your-own-key. You cannot enter, upload, store or
configure a provider API key, and requests that attempt to supply one are
rejected by the service rather than merely hidden in the interface.

This is deliberate. Youtab holds the credentials, the capacity and the
contractual data-protection terms centrally, so that every account gets the
same vetted processing path rather than one assembled per user.

If you have an enterprise or on-premise requirement that depends on your own
infrastructure, contact Youtab — that is handled as a contracted arrangement
under organization administration, not as a per-user setting.

## Third-party processing

To deliver and operate the service, Youtab may use carefully selected
third-party infrastructure and AI processing service providers. These providers
are subject to applicable contractual, confidentiality, security and
data-protection requirements.

Youtab does not publish which provider serves which agent. The engine behind an
agent is implementation infrastructure: it is versioned, it is changed when
Youtab has reason to change it, and pinning it in public documentation would
turn an operational detail into a promise. What is stable, and what you build
against, is the agent's public identity — `alpha.v06`, labelled **Alpha v0.6** —
together with its documented role and capabilities.

For the full statement of how Youtab handles your data, see the
[Terms of Service](https://youtab.io/terms) and the
[Privacy Policy](https://youtab.io/privacy).

## What replaced this page

This page used to be a catalogue of inference providers and the environment
variables needed to authenticate against each one. That reflected an earlier
shape of the product, in which every user assembled their own inference path.
Youtab is a managed service now: the catalogue described a capability normal
users no longer have, so keeping it would have documented a setup that cannot
be performed.

Agent identity, selection and capabilities are documented in
[Configuring Agents](../user-guide/configuring-models.md).
