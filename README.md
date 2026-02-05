# smart email assistant

a self-hosted email automation system built with n8n and ollama.  
it reads incoming emails, understands intent using a local llm, replies automatically when safe, schedules meetings, and keeps full audit logs.

the system runs fully on your infrastructure.

no external ai api keys.

---

## what this project does

you receive emails every day.  
most of them follow patterns.

this project automates that flow.

it:

- reads incoming emails from gmail
- classifies intent using ollama llama 3.2 1b
- decides whether to reply, schedule a meeting, or stop
- sends replies through gmail
- creates google calendar events with google meet links
- logs every action in google sheets
- posts execution traces to slack
- sends low-confidence emails to manual review

you control the data.  
you control the model.  
you control the workflow.

---

## supported email intents

each email is classified into one category:

- general_inquiry  
- support_request  
- meeting_request  
- spam  
- out_of_office  

spam and out_of_office are logged only.  
no automated replies are sent.

---

## high-level flow

incoming email
|
v
n8n workflow
|
email preprocessing
|
ollama classification
|
intent + confidence
/ |
reply log meeting flow
|
google calendar + meet


manual review triggers when confidence < 0.7.

---

## why this project exists

most email automations depend on cloud llms.  
that creates cost risk and data exposure.

this project solves that by:

- running llm inference locally with ollama
- using a small 1b model that fits modest hardware
- keeping logic transparent inside n8n
- making decisions traceable and auditable

it works well for internal teams, startups, and ops-heavy workflows.

---

## tech stack

core automation:

- n8n self-hosted

llm and inference:

- ollama
- llama 3.2 1b

email and scheduling:

- gmail api
- google calendar api
- google meet

logging and monitoring:

- google sheets
- slack

---

## requirements

you need:

- a local machine or server
- docker or native runtime
- n8n community edition
- ollama installed locally
- google workspace account
- slack workspace

no paid services required.

---

## installation summary

1. install n8n  
2. install ollama  
3. pull llama 3.2 1b  
4. configure google oauth credentials  
5. configure slack bot token  
6. set environment variables  
7. import n8n workflows  
8. start processing emails  

the system runs continuously once deployed.

---

## environment variables

example:

N8N_BASIC_AUTH_ACTIVE=true
N8N_BASIC_AUTH_USER=admin
N8N_BASIC_AUTH_PASSWORD=changeme
OLLAMA_HOST=127.0.0.1
OLLAMA_PORT=11434


secrets should be stored using n8n credentials in production.

---

## classification logic

the model returns:

- intent
- confidence score from 0 to 1
- short reasoning

decision rules:

- confidence >= 0.85  
  automatic action

- 0.7 <= confidence < 0.85  
  automatic action with review flag

- confidence < 0.7  
  manual review only

this avoids risky replies.

---

## meeting automation

when a meeting request is accepted:

- an ics invite is generated
- a google calendar event is created
- a google meet link is attached
- timezone is fixed to africa cairo
- email reply is sent automatically

calendar errors are avoided using explicit tz handling.

---

## logging and auditing

every email creates a log entry.

stored fields include:

- sender
- subject
- intent
- confidence
- action taken
- timestamps
- meeting links if created
- manual review flags

logs live in google sheets.  
easy to audit.  
easy to export.

---

## who should use this

you should use this project if:

- you want email automation without cloud llms
- you need explainable decisions
- you care about data locality
- you already use google workspace
- you want full control over workflows
