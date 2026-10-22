"""The foundation's welcome page."""
import streamlit as st


def show_welcome() -> None:
    st.set_page_config(page_title="CloudSync", page_icon="☁️")
    st.title("CloudSync")
    st.subheader("Phase 1: Full-copy file synchronization")
    st.write(
        "Phase 1 will scan a local source folder, divide files into fixed-size "
        "binary chunks, record metadata in SQLite, and copy chunks to a local "
        "folder or AWS S3 with progress displayed in a web dashboard."
    )
    st.info("Foundation ready. Scanning, chunk uploads, and the dashboard are future work.")
    st.caption("Phase 2 will add incremental synchronization.")
