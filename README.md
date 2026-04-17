
# Kubernetes ML Inference + Autoscaling System

A production-style ML inference service deployed on Kubernetes with CPU-based autoscaling using Horizontal Pod Autoscaler (HPA).

---

## What it does

A FastAPI ML inference service that runs sentiment analysis on text input using a DistilBERT model (`distilbert-base-uncased-finetuned-sst-2-english` from HuggingFace).

**API:**
- `POST /predict` — accepts `{ "input": "<text>" }`, returns `{ "label": "POSITIVE"|"NEGATIVE", "score": 0.99 }`

**Stack:**
- FastAPI + Uvicorn as the web server
- HuggingFace `transformers` + PyTorch for inference
- Containerized with Docker (`python:3.10-slim`, port 8000)

**Kubernetes setup:**
- `deployment.yaml` — 1 replica, CPU requests: 200m / limits: 500m, `imagePullPolicy: Never` (uses local image)
- `service.yaml` — NodePort service, external port 80 → container port 8000

The project demonstrates **Kubernetes autoscaling** of an ML API — the CPU limits on the Deployment are configured to trigger HPA (Horizontal Pod Autoscaler) under load.

---

## Overview

This system runs an ML-style inference API on Kubernetes and automatically scales based on traffic load.

- FastAPI-based inference API (model serving layer)
- Dockerized application for consistent runtime deployment
- Kubernetes Deployment + Service for orchestration and networking
- Horizontal Pod Autoscaler (HPA) for dynamic scaling
- Metrics Server for resource utilization tracking
- Load testing for validating scaling behavior
- Automatic pod scaling under traffic load

---

## Architecture

```text
User Traffic → Kubernetes Service → Pods (Inference API)
→ Metrics Server → HPA → Dynamic Pod Scaling


code → docker → kubernetes deployment → service → (next: autoscaling)