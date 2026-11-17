<!-- Used by: the job-search app's parser, as the shape of one `jobs.parse_report` call for a recipe written from a site's recorded traffic.
     Loaded: by the app's hand-out of a recording (`jobs.parse_next`, backend/app/jobsearch/role_parse.py); named by instructions/jobsearch.parser.recipe.instructions.md.
     Placeholders: none. Context keys: none. -->
id: "The id, exactly as jobs.parse_next gave it"
fields:
  recipe:
    kind: request
    method: GET
    url: "https://careers.example.test/api/jobs"
    params:
      offset: "{offset}"
      limit: "{size}"
    page:
      param: offset
      size: 20
    items: "results"
    fields:
      title: "name"
      url: "path"
      ext_id: "reqId"
      location: "places"
      posted: "opened"
    transforms:
      location:
        - op: join
          sep: " / "
    url_prefix: "https://careers.example.test"
# or, when no data request carried the listing and the page holds the data as a block (embedded_in_page):
#   recipe:
#     kind: embedded
#     method: GET
#     url: "https://careers.example.test/jobs"
#     blob: "script:__DATA__"
#     params: {page: "{page}"}
#     page: {param: page, size: 10}
#     items: "results.jobs"
#     fields: {title: "title", url: "path", ext_id: "id"}
#     url_prefix: "https://careers.example.test"
# or, when no recipe can be made:
# fields:
#   none: "The only request that returns postings carries a token a script on the page makes."
# or, when the text is the traffic of ONE posting's page (the job's own page on an employer's site), the recipe for
# that host's postings:
#   recipe:
#     kind: posting
#     host: "careers.example.test"
#     url_pattern: "https://careers.example.test/job/(?P<ext_id>R-\d+)"
#     detail:
#       url: "https://careers.example.test/api/posting/{ext_id}"
#       answer: json
#       description: "body"
#       transforms:
#         - op: html_text
