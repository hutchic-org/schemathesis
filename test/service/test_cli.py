import re
from test.apps.openapi.schema import OpenAPIVersion

import pytest
from _pytest.main import ExitCode
from requests import Timeout

import schemathesis
from schemathesis.cli.output.default import SERVICE_ERROR_MESSAGE
from schemathesis.constants import USER_AGENT
from schemathesis.service.constants import REPORT_CORRELATION_ID_HEADER
from schemathesis.service.hosts import load_for_host

from ..utils import strip_style_win32


@pytest.fixture(autouse=True)
def reset_hooks():
    yield
    schemathesis.hooks.unregister_all()


def get_stdout_lines(stdout):
    return [strip_style_win32(line) for line in stdout.splitlines()]


@pytest.mark.operations("success")
@pytest.mark.parametrize("openapi_version", (OpenAPIVersion("3.0"),))
def test_no_failures(cli, schema_url, service, next_url, upload_message):
    # When Schemathesis.io is enabled and there are no errors
    result = cli.run(
        schema_url,
        "my-api",
        f"--schemathesis-io-token={service.token}",
        f"--schemathesis-io-url={service.base_url}",
        "--report",
    )
    assert result.exit_code == ExitCode.OK, result.stdout
    # Then it should receive requests
    assert len(service.server.log) == 2, service.server.log
    # And all requests should have the proper User-Agent
    for (request, _) in service.server.log:
        assert request.headers["User-Agent"] == USER_AGENT
    # Create test run
    service.assert_call(0, "/apis/my-api/", 200)
    service.assert_call(1, "/reports/upload/", 202)
    # And it should be noted in the output
    lines = get_stdout_lines(result.stdout)
    # This output contains all temporary lines with a spinner - regular terminals handle `\r` and display everything
    # properly. For this test case, just check one line
    assert "Upload: COMPLETED" in lines
    assert upload_message in lines
    assert next_url in lines


@pytest.mark.operations("success")
@pytest.mark.service(data={"detail": "Internal Server Error"}, status=500, method="POST", path="/reports/upload/")
@pytest.mark.parametrize("openapi_version", (OpenAPIVersion("3.0"),))
def test_server_error(cli, schema_url, service):
    # When Schemathesis.io is enabled but returns 500 on the first call
    args = [
        schema_url,
        "my-api",
        f"--schemathesis-io-token={service.token}",
        f"--schemathesis-io-url={service.base_url}",
        "--report",
    ]
    result = cli.run(*args)
    assert result.exit_code == ExitCode.OK, result.stdout
    assert len(service.server.log) == 2
    service.assert_call(0, "/apis/my-api/", 200)
    service.assert_call(1, f"/reports/upload/", 500)
    # And it should be noted in the output
    lines = get_stdout_lines(result.stdout)
    assert "Upload: ERROR" in lines
    assert "Please, try again in 30 minutes" in lines


@pytest.mark.operations("success")
@pytest.mark.parametrize("openapi_version", (OpenAPIVersion("3.0"),))
def test_error_in_another_handler(testdir, cli, schema_url, service):
    # When a non-Schemathesis.io handler fails
    module = testdir.make_importable_pyfile(
        hook="""
        import click
        import schemathesis
        from schemathesis.cli.handlers import EventHandler
        from schemathesis.runner import events

        class FailingHandler(EventHandler):

            def handle_event(self, context, event):
                1 / 0

        @schemathesis.hooks.register
        def after_init_cli_run_handlers(
            context,
            handlers,
            execution_context
        ):
            handlers.append(FailingHandler())
        """
    )
    result = cli.main(
        "--pre-run",
        module.purebasename,
        "run",
        schema_url,
        "my-api",
        f"--schemathesis-io-token={service.token}",
        f"--schemathesis-io-url={service.base_url}",
    )
    # And all handlers are shutdown forcefully
    # And the run fails
    assert result.exit_code == ExitCode.TESTS_FAILED, result.stdout


@pytest.mark.operations("success")
@pytest.mark.parametrize("openapi_version", (OpenAPIVersion("3.0"),))
def test_server_timeout(cli, schema_url, service, mocker):
    # When Schemathesis.io responds slowly
    mocker.patch("schemathesis.service.WORKER_FINISH_TIMEOUT", 0)
    # And the waiting is more than allowed
    result = cli.run(
        schema_url,
        "my-api",
        f"--schemathesis-io-token={service.token}",
        f"--schemathesis-io-url={service.base_url}",
        "--report",
    )
    assert result.exit_code == ExitCode.OK, result.stdout
    # Then the output indicates timeout
    lines = get_stdout_lines(result.stdout)
    assert lines[18].endswith("Upload: TIMEOUT")


@pytest.mark.service(
    data={"title": "Unauthorized", "status": 401, "detail": "Could not validate credentials"},
    status=401,
    method="GET",
    path=re.compile("/apis/.*/"),
)
@pytest.mark.parametrize("openapi_version", (OpenAPIVersion("3.0"),))
def test_unauthorized(cli, schema_url, service):
    # When the token is invalid
    result = cli.run(
        schema_url, "my-api", "--schemathesis-io-token=invalid", f"--schemathesis-io-url={service.base_url}"
    )
    assert result.exit_code == ExitCode.TESTS_FAILED, result.stdout
    # Then a proper error message should be displayed
    lines = get_stdout_lines(result.stdout)
    assert "Please, check that you use the proper CLI access token" in lines


@pytest.mark.service(
    data={"title": "Bad request", "status": 400, "detail": "Something wrong"},
    status=400,
    method="POST",
    path="/reports/upload/",
)
@pytest.mark.parametrize("openapi_version", (OpenAPIVersion("3.0"),))
def test_invalid_payload(cli, schema_url, service):
    # When there is no token or invalid token
    result = cli.run(
        schema_url,
        "my-api",
        f"--schemathesis-io-token={service.token}",
        f"--schemathesis-io-url={service.base_url}",
        "--report",
    )
    assert result.exit_code == ExitCode.TESTS_FAILED, result.stdout
    # Then a proper error message should be displayed
    lines = get_stdout_lines(result.stdout)
    assert f"{SERVICE_ERROR_MESSAGE}:" in lines
    assert "Please, consider" in result.stdout
    assert "Response: " in result.stdout
    assert "400 Client Error" in result.stdout


@pytest.mark.parametrize("openapi_version", (OpenAPIVersion("3.0"),))
def test_connection_issue(cli, schema_url, service, mocker):
    # When there is a connection issue
    mocker.patch("schemathesis.service.report.serialize_event", side_effect=Timeout)
    result = cli.run(
        schema_url,
        "my-api",
        f"--schemathesis-io-token={service.token}",
        f"--schemathesis-io-url={service.base_url}",
        "--report",
    )
    assert result.exit_code == ExitCode.TESTS_FAILED, result.stdout
    # Then a proper error message should be displayed
    lines = get_stdout_lines(result.stdout)
    assert f"{SERVICE_ERROR_MESSAGE}:" in lines
    assert "Please, consider" not in result.stdout
    assert "Timeout" in result.stdout


@pytest.mark.parametrize("openapi_version", (OpenAPIVersion("3.0"),))
def test_api_id_no_token(cli, schema_url, hosts_file):
    # When there is API ID
    # And there is no token
    result = cli.run(schema_url, "my-api", f"--hosts-file={hosts_file}")
    # Then it should be an error
    assert result.exit_code == ExitCode.INTERRUPTED, result.stdout
    assert "CLI appears to be not authenticated" in result.stdout


@pytest.mark.service(
    data={"title": "Not found", "status": 404, "detail": "Resource not found"},
    status=404,
    method="GET",
    path=re.compile("/apis/.*/"),
)
@pytest.mark.parametrize("openapi_version", (OpenAPIVersion("3.0"),))
def test_invalid_api_name(cli, schema_url, service):
    # When API name does not exist
    result = cli.run(
        schema_url, "my-api", f"--schemathesis-io-token={service.token}", f"--schemathesis-io-url={service.base_url}"
    )
    assert result.exit_code == ExitCode.TESTS_FAILED, result.stdout
    # Then the error should be immediately visible
    assert result.stdout.strip() == "❌ API with name `my-api` not found!"


def test_not_authenticated_with_name(cli):
    # When the user is not authenticated
    # And uses an API name
    result = cli.run("my-api")
    # Then the error message should note it
    assert result.exit_code == ExitCode.INTERRUPTED, result.stdout
    assert "You are trying to upload data to" in result.stdout.strip()


def test_two_names(cli, service):
    # When the user passes api name twice
    result = cli.run(
        "my-api", "my-api", f"--schemathesis-io-token={service.token}", f"--schemathesis-io-url={service.base_url}"
    )
    # Then the error message should note it
    assert result.exit_code == ExitCode.INTERRUPTED, result.stdout
    assert result.stdout.strip().endswith("Got unexpected extra argument (my-api)")


@pytest.mark.operations("success")
def test_authenticated_with_name(cli, service):
    # When the user is authenticated
    # And uses an API name
    result = cli.run("my-api", f"--schemathesis-io-token={service.token}", f"--schemathesis-io-url={service.base_url}")
    # Then the schema location should be loaded
    assert result.exit_code == ExitCode.OK, result.stdout
    assert "1 passed" in result.stdout


@pytest.mark.parametrize("openapi_version", (OpenAPIVersion("3.0"),))
@pytest.mark.operations("success")
def test_permission_denied_on_hosts_creation(mocker, cli, schema_url, service, hosts_file):
    # When the hosts file can't be created
    mocker.patch("pathlib.Path.mkdir", side_effect=PermissionError)
    # Then it should not make the run fail
    result = cli.run(schema_url, f"--hosts-file={hosts_file}")
    assert result.exit_code == ExitCode.OK, result.stdout


@pytest.mark.operations("success")
@pytest.mark.service(data={"username": "TestUser"}, status=200, method="POST", path="/auth/cli/login/")
@pytest.mark.parametrize("openapi_version", (OpenAPIVersion("3.0"),))
def test_anonymous_upload(cli, schema_url, service, hosts_file, correlation_id):
    # When upload is anonymous
    result = cli.run(schema_url, f"--schemathesis-io-url={service.base_url}", f"--hosts-file={hosts_file}", "--report")
    # Then it is successful
    assert result.exit_code == ExitCode.OK, result.stdout
    assert SERVICE_ERROR_MESSAGE not in result.stdout
    service.assert_call(0, "/reports/upload/", 202)
    # And the returned correlation id should be properly stored
    assert load_for_host(service.hostname, hosts_file)["correlation_id"] == correlation_id
    # And the same correlation id is used for the next upload
    cli.run(schema_url, f"--schemathesis-io-url={service.base_url}", f"--hosts-file={hosts_file}", "--report")
    assert service.server.log[1][0].headers[REPORT_CORRELATION_ID_HEADER] == correlation_id
    # And later auth should not override existing correlation_id
    result = cli.auth.login(
        "sample_token", f"--hosts-file={hosts_file}", f"--hostname={service.hostname}", "--protocol=http"
    )
    # Then the command succeeds
    assert result.exit_code == 0, result.stdout
    assert load_for_host(service.hostname, hosts_file)["correlation_id"] == correlation_id
