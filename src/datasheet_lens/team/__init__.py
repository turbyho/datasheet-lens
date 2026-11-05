"""The team server: one shared store for the datasheets of a team.

The server keeps only data: PostgreSQL for the rows, an S3 object store (Garage)
for the files. The clients do all processing. Commands: dslens-server (setup,
run) and dslens-admin (accounts, tokens). Install with datasheet-lens[server].
"""
