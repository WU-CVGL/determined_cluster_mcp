from determined_compute.utils.secrets import default_secrets_path, load_secrets


def test_secrets_path_prefers_explicit_file_then_environment_then_working_directory(tmp_path, monkeypatch):
    monkeypatch.delenv('DETERMINED_COMPUTE_SECRETS', raising=False)
    monkeypatch.chdir(tmp_path)
    assert default_secrets_path() == tmp_path / '.determined_compute.env'
    assert load_secrets() == {}

    from_environment = tmp_path / 'credentials.env'
    from_environment.write_text('DET_MASTER=https://cluster.example\nDET_API_TOKEN=test-value\n')
    monkeypatch.setenv('DETERMINED_COMPUTE_SECRETS', str(from_environment))
    assert default_secrets_path() == from_environment
    assert load_secrets()['DET_MASTER'] == 'https://cluster.example'

    explicit = tmp_path / 'explicit.env'
    explicit.write_text('DET_USERNAME=example-user\n')
    assert load_secrets(explicit) == {'DET_USERNAME': 'example-user'}


def test_quoted_ssh_credentials_are_literal_not_shell_code(tmp_path):
    path = tmp_path / 'credentials.env'
    path.write_text("export SSH_USERNAME='example-user'\nSSH_PASSWORD=\"$literal=$(never-run)#value\"\n")
    assert load_secrets(path) == {
        'SSH_USERNAME': 'example-user',
        'SSH_PASSWORD': '$literal=$(never-run)#value',
    }
