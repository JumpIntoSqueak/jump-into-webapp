from flask import Flask, url_for, redirect, request, abort, render_template
from flask_wtf import Form, RecaptchaField
from flask_wtf.csrf import CsrfProtect
from wtforms import TextField

import subprocess
import random
import httplib
import docker
import ipaddress
import json
from docker.errors import APIError
from requests.exceptions import Timeout
import requests

from celery_task import make_celery
from settings import *

app = Flask("webapp")
app.config.update(
    CELERY_BROKER_URL='sqla+sqlite:///celerydb.sqlite',
    CELERY_RESULT_BACKEND='db+sqlite:///results.sqlite',
    SECRET_KEY=SECRET_KEY,
    RECAPTCHA_PUBLIC_KEY=RECAPTCHA_PUBLIC_KEY,
    RECAPTCHA_PRIVATE_KEY=RECAPTCHA_PRIVATE_KEY
)
celery = make_celery(app)


class CapchaForm(Form):
    recaptcha = RecaptchaField()


@app.route('/<user>/<repository>', methods=('GET', 'POST'))
def github(user, repository):
    if len(running_instances()) >= MAX_INSTANCES:
        return "Maximum number of concurrent instances reached"

    captcha = CapchaForm()

    if captcha.validate_on_submit() == False:
        return render_template('captcha.html', form=captcha)

    check_repo = check_repository(user, repository)
    if check_repo is not True:
        return check_repo

    result = live_instace.delay(user, repository)
    return redirect('http://' + NOVNC_HOST + url_for('status_for', id=result.id))


@app.route('/hooks/', methods=['POST'])
def update_github_cache():
    hook_blocks = requests.get('https://api.github.com/meta').json()['hooks']
    for block in hook_blocks:
        ip = ipaddress.ip_address(u'%s' % request.remote_addr)
        if ipaddress.ip_address(ip) in ipaddress.ip_network(block):
            break  # the remote_addr is within the network range of github
        else:
            abort(403)
        if request.headers.get('X-GitHub-Event') != "push":
            return "Wrong event type"

    payload = json.loads(request.data)

    repository = payload['repository']['name']
    user = payload['repository']['owner']['name']

    check_repo = check_repository(user, repository)
    if check_repo is not True:
        return check_repo

    update_build_cache_for(user, repository, "HEAD")
    return "Updating Cache"


def check_repository(user, repository):
    if not repository_allowed(user, repository):
        return "Repository not allowed"
    if not repository_exists(user, repository):
        return "Repository does not exist"
    return True


def repository_allowed(user, repository):
    repo = "https://github.com/%s/%s" % (user, repository)
    for r in GH_REPOSITORIES:
        if repo.startswith(r):
            print repo + " starts with " + r
            return True
    return False


def repository_exists(user, repository):
    url = "/" + user + "/" + repository
    try:
        conn = httplib.HTTPSConnection("github.com")
        conn.request("HEAD", url)
        r = conn.getresponse()
        r.read()
        return 200 == r.status
    except StandardError as e:
        print e
        return None


@app.route('/status/<id>')
def status_for(id):
    r = live_instace.AsyncResult(id)
    if r.ready():
        return redirect(
            'http://{0:s}/static/noVNC/vnc.html?autoconnect=true&host={1:s}&password=1234&path=&port={2:d}&id={3:s}'.format(
                NOVNC_HOST, DOCKER_HOST, r.get()['VNCPort'], id))
    else:
        return '<script>setTimeout(function(){window.location.reload(1);}, 10000);</script>booting up'


@app.route('/image/<id>')
def get_image_for(id):
    r = live_instace.AsyncResult(id)
    if r.ready():
        return redirect('http://{0:s}:{1:d}/Squeak4.5-13680.image'.format(DOCKER_HOST, r.get()['HTTPPort']))
    return "not ready yet"


@app.route('/changes/<id>')
def get_changes_for(id):
    r = live_instace.AsyncResult(id)
    if r.ready():
        return redirect('http://{0:s}:{1:d}/Squeak4.5-13680.changes'.format(DOCKER_HOST, r.get()['HTTPPort']))
    return "not ready yet"


@celery.task(track_started=True)
def delete_instance(container):
    client = get_docker_connection()
    try:
        client.stop(container, timeout=1.5)
    except Timeout:
        pass
    try:
        client.remove_container(container)
    except Timeout:
        pass


@celery.task(track_started=True)
def live_instace(user, repository):
    commit = build_image(user, repository)
    return run_image(user, repository, commit)


@celery.task
def update_build_cache_for(user, repository, commit):
    project = "%s/%s" % (user, repository)
    subprocess.check_call(["docker", "build", "-t", project.lower().replace("-", "_") + ":" + commit,
                           "https://github.com/" + project + ".git"])


def build_image(user, repository, commit="HEAD"):
    try:
        use_image_cache_for(user, repository, commit)

    except subprocess.CalledProcessError as e:
        print "[ERROR] Could not build image: " + str(e)

    return commit


def use_image_cache_for(user, repository, commit):
    project = "%s/%s" % (user, repository)
    tag = project.lower().replace("-", "_").replace(".", "_")

    p = subprocess.check_output(["docker", "images"])
    for line in p.split('\n'):
        if tag in line:
            if ' ' + commit + ' ' in line:
                print "cache hit for", project, ":", commit
                return commit
    update_build_cache_for(user, repository, commit)


def choose_name(user, repository, commit):
    instance = user + "-" + repository + "-" + str(random.randint(1, 2 * MAX_INSTANCES)) + "-" + commit
    if instance in [e['Name'] for e in running_instances()]:
        instance = choose_name(user, repository, commit)
    return instance


def choose_port():
    ports = used_ports()
    http_port, vnc_port = random.sample(xrange(MIN_PORT, MAX_PORT), 2)
    if http_port in ports or vnc_port in ports:
        http_port, vnc_port = choose_port()
    return http_port, vnc_port


def run_image(user, repository, commit):
    project = "%s/%s" % (user, repository)
    image = project.lower().replace("-", "_") + ":" + commit

    instance_name = choose_name(user, repository, commit)
    http_port, vnc_port = choose_port()

    client = get_docker_connection()
    try:
        container = client.create_container(image, hostname=instance_name,
                                            detach=True, mem_limit="512m",
                                            ports=[VNCPORT, HTTPPORT], name=instance_name,
                                            entrypoint=None, cpu_shares=100)
        client.start(container, port_bindings={VNCPORT: vnc_port, HTTPPORT: http_port})
        delete_instance.apply_async([container], countdown=3660)
    except APIError as e:
        print e, repr(running_instances()), instance_name

    return {'HTTPPort': http_port, 'VNCPort': vnc_port}


def get_docker_connection():
    return docker.Client(base_url='unix://var/run/docker.sock',
                         version='1.14',
                         timeout=10)


def running_instances():
    client = get_docker_connection()
    containers = client.containers(quiet=False, all=False, trunc=True, latest=False, since=None,
                                   before=None, limit=-1)
    results = []
    for c in containers:
        e = {'Name': c['Names'][0]}
        if len(c['Ports']) != 2:
            continue  # instance started outside of webapp
        e['HTTPPort'] = c['Ports'][0]['PublicPort']
        e['VNCPort'] = c['Ports'][1]['PublicPort']
        if c['Ports'][0]['PrivatePort'] == VNCPORT:
            e['HTTPPort'], e['VNCPort'] = e['VNCPort'], e['HTTPPort']
        results.append(e)
    return results


def used_ports():
    instances = running_instances()
    return [e['HTTPPort'] for e in instances] + [e['VNCPort'] for e in instances]


if __name__ == '__main__':
    with open('github/allowed_repositories') as f:
        GH_REPOSITORIES = f.read().splitlines()
        GH_REPOSITORIES = [r for r in GH_REPOSITORIES if r.strip() != '']

    with app.test_request_context():
        print url_for('github', user='hubx', repository='SWA-BAttack')
        print repository_exists('hubx', 'SWA-BAttack')
        print repository_exists('hubx', 'SWA-BAttacks')
    CsrfProtect(app)
    app.run(host='0.0.0.0', port=HTTPPORT, debug=True)

