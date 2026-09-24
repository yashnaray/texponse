# Texponse 

## Project Summary 
An AI assistant that automatically replies to customer text messages and remembers the conversation.

## What I did (short version)
1. **Got the app starting.** Fixed three startup crashes (outdated address-book code for the knowledge database, broken user-account definitions, missing ID numbers on records).
2. **Got answers generating.** Connected the AI pipeline end to end: incoming text → knowledge search → AI-written reply → saved reply. The AI answers questions in ~4 seconds.
3. **Connected real texting (Aircall).** Found and fixed the correct Aircall send method, configured phone line 1244782 for automated texting, and proved a live inbound text ("Hi hi") flows all the way through the system.
4. **Made results visible.** The test button now waits and returns the actual answer (or the exact error) instead of just "queued."
5. **Cleaned up.** All automated checks pass 

## Where things stand
- ✅ App runs, AI replies, replies are stored.
- ✅ Outbound texting path works (one test number texting itself is blocked by Aircall — that's their rule, not a bug).
- ⏳ Inbound auto-replies need the callback pointed at the new `/webhooks/aircall/callback` address.

## Why dev accounts / keys are needed
The app doesn't work standalone — it rents three outside services, and each one needs an account key to let us in:

| Service | What it does | What we needed |
|---|---|---|
| **OpenRouter** | The AI brain (writes replies) | An API key (pasted). Old AI model names had expired, so we switched to the `openrouter/free` router. |
| **Aircall** | Sends/receives the actual text messages | API login (done) + one phone number switched on for automated texting (done, line 1244782). A number can only text via the app *or* via automation, not both. That's Aircall's rule. |
| **Jobber** | Looks up who the customer is (name, jobs, history) so replies are personal | A **developer app** (not yet created). This is required : without it, the AI answers blindly with no customer context. |
| **ngrok** | A temporary public address so Aircall can reach the laptop during testing | Free signup + one token (done). |

## The one thing left: Jobber
To give the AI customer context, someone needs to (1) create a free Jobber developer app, (2) approve it once in a browser, and (3) save the resulting login token in our database. After that, every incoming text automatically includes the customer's Jobber record in the AI's answer. No code changes needed — just the account + one approval click.
