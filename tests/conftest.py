import os

# Technologies added with `chipleap tech add` live in the user's config directory and
# would change exploration results; the tests run on the built-in ones only.
os.environ["CHIPLEAP_TECH_DIR"] = ""
