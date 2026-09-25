// Generated from eas_provider/workflow.yml before dev/build.
// prettier-ignore
export const easWorkflowTemplate = "name: Harbor sandbox\ndefaults:\n  tools:\n    node: \"24\"\njobs:\n  sandbox:\n    runs_on: macos-medium\n    image: macos-tahoe-26.4-xcode-26.4\n    steps:\n      - uses: eas/checkout\n      - name: Prepare Harbor sandbox\n        run: bash bootstrap.sh\n";
