# Aither World Services — public images

The container images behind **Aither World Services**, the open-source private cloud you
run on hardware you own: identity, directory, secrets, object storage, metrics, audit
trail, events, messaging and compute.

Images are published as `ghcr.io/aitherium/aws-*` and pull with no login.

Each image is a single-layer build from an AitherOS service image, scanned layer by layer
before it is published. `.github/workflows/publish.yml` builds them.

Read more: https://blog.aitherium.com/blog/aither-world-services-your-own-private-ai-cloud
