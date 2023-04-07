#!/bin/bash

ret_code=0

install_dependencies(){
    if [ "$RUNNER_OS" == "Linux" ]; then
        # https://github.com/pypa/setuptools/issues/3269
        export DEB_PYTHON_INSTALL_LAYOUT=deb
    fi
    python3 -m pip install -r requirements.txt
}

run_commit_check(){
    args=""
    if [ "$MESSAGE" == "true" ]; then
        args="$args --message"
    fi
    if [ "$BRANCH" == "true" ]; then
        args="$args --branch"
    fi
    if [ "$AUTHOR_NAME" == "true" ]; then
        args="$args --author-name"
    fi
    if [ "$AUTHOR_EMAIL" == "true" ]; then
        args="$args --author-email"
    fi

    echo "commit-check $args"
    commit-check $args > result.txt
    ret_code=$?
}

add_job_summary(){
    if [ "$JOB_SUMMARY" == "false" ]; then
        exit
    fi

    if [ -s result.txt ]; then
        # strips ANSI colors
        sed -i "s,\x1B\[[0-9;]*[a-zA-Z],,g" result.txt
        cat result.txt
        echo "### Commit-Check ❌" >> "$GITHUB_STEP_SUMMARY"
        echo '```' >> "$GITHUB_STEP_SUMMARY"
        cat result.txt >> "$GITHUB_STEP_SUMMARY"
        echo '```' >> "$GITHUB_STEP_SUMMARY"
        ret_code=1
    else
        echo "### Commit-Check ✔️" >> "$GITHUB_STEP_SUMMARY"
        ret_code=0
    fi
}

install_dependencies
run_commit_check
add_job_summary

if [ "$DRY_RUN" == "true" ]; then
    ret_code=0
fi

exit $ret_code
