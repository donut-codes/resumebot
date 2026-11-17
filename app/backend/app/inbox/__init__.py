"""The Inbox and the task list: what needs the owner, what the app has to tell them, and what is on their list.

A **task** and a **notification** are this app's own small tables (`app.models.work`); `service` reads and changes them,
`events` raises them for the things a search produces, and `routes` is the API the Inbox and Tasks screens call.
"""
