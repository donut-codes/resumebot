"""resumebot 1.0.0 (job search): a scout agent searches job boards and scores roles against the owner's match profile;
this app de-duplicates the roles against the owner's existing tracker, applies the owner's hard gates, stores every
role (kept, or screened out with the reason), shows them on one page and exports them to a spreadsheet in the
tracker's column order. It builds no resumes and applies to nothing.

It runs on the process engine package (`process_engine`, vendored from another project's code; running
`playbooks/jobsearch/*.bpmn` live, with no shadow), the MCP SDK and the agent token hashing (`app.agents.security`), the
agent payload budget (`app.planengine.budget`) and the instruction and template files (`instructions/jobsearch.*`,
`templates/jobsearch.*`). `app/PROVENANCE.md` names the upstream commit each copied module came from.

Run it: `uvicorn app.jobsearch.main:app` with `JOBSEARCH_DATABASE_URL` set. Its tables are made on start
(`app.db.init_schema`), on a metadata of their own.
"""
