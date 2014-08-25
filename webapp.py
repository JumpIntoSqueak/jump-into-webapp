from flask import Flask, url_for, redirect
import subprocess
import random
import httplib

from celery_task import make_celery

app = Flask("webapp")
app.config.update(
    CELERY_BROKER_URL='sqla+sqlite:///celerydb.sqlite',
    CELERY_RESULT_BACKEND='db+sqlite:///results.sqlite'
)
celery = make_celery(app)

# Private Ports for Docker Instances
VNCPORT = 8080
HTTPPORT = 80

MIN_PORT = 1024
MAX_PORT = 49151


@app.route('/<user>/<repository>')
def github(user, repository):
    if not repository_allowed(user, repository):
        return "repository not allowed"
    if not repository_exists(user, repository):
        return "repository does not exist"
    result = live_instace.delay(user, repository)
    return redirect(url_for('status_for', id=result.id))


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
            'http://localhost:5000/static/noVNC/vnc.html?autoconnect=true&host=localhost&password=1234&path=&port=' +
            str(r.get()['VNCPort']) + "&id=" + id)
    else:
        return '<script>setTimeout(function(){window.location.reload(1);}, 10000);</script>booting up'


@app.route('/image/<id>')
def get_image_for(id):
    r = live_instace.AsyncResult(id)
    if r.ready():
        return redirect("http://localhost:" + str(r.get()['HTTPPort']) + "/Squeak4.5-13680.image")
    return "not ready yet"


@app.route('/changes/<id>')
def get_changes_for(id):
    r = live_instace.AsyncResult(id)
    if r.ready():
        return redirect("http://localhost:" + str(r.get()['HTTPPort']) + "/Squeak4.5-13680.changes")
    return "not ready yet"


@celery.task(track_started=True)
def delete_instance(instance):
    try:
        subprocess.check_call(["sudo", "docker.io", "stop",
                               instance])
        subprocess.check_call(["sudo", "docker.io", "rm",
                               instance])
    except subprocess.CalledProcessError as e:
        print "[ERROR] Could not stop image: " + str(e)


@celery.task(track_started=True)
def live_instace(user, repository):
    commit = build_image(user, repository)
    return run_image(user, repository, commit)


def build_image_cache(user, repository, commit):
    project = "%s/%s" % (user, repository)

    p = subprocess.check_output(['sudo', "docker.io", "images"])
    for line in p.split('\n'):
        if project.lower() in line:
            if ' ' + commit + ' ' in line:
                print "cache hit for", project, ":", commit
                return
            # XX add real commit, HEAD will result in false positives cache hits
    subprocess.check_call(["sudo", "docker.io", "build", "-t", project.lower() + ":" + commit,
                           "https://github.com/" + project + ".git"])


def build_image(user, repository, commit="HEAD"):
    try:
        build_image_cache(user, repository, commit)

    except subprocess.CalledProcessError as e:
        print "[ERROR] Could not build image: " + str(e)

    return commit


def choose_name(user, repository, commit):
    instance = user + "-" + repository + "-" + str(random.randint(1, 2*MAX_INSTANCES)) + "-" + commit
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
    instance = choose_name(user, repository, commit)
    http_port, vnc_port = choose_port()

    try:
        subprocess.check_call(["sudo", "docker.io", "run", "-d",
                               "--name", instance,
                               "-p", str(vnc_port) + ":8080",
                               "-p", str(http_port) + ":80",
                               "-c", "100",  # equals 10% cpu shares
                               project.lower() + ":" + commit])
        print http_port, vnc_port

    except subprocess.CalledProcessError as e:
        print "[ERROR] Could not start image: " + str(e)

    delete_instance.apply_async([instance], countdown=3660)
    return {'HTTPPort': http_port, 'VNCPort': vnc_port}


def running_instances():
    import docker

    client = docker.Client(base_url='unix://var/run/docker.sock',
                           version='1.14',
                           timeout=10)
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
    app.run(debug=True)

