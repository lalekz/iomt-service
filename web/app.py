import mongoengine
from flask import Flask, render_template, request, redirect, session, send_file, jsonify, url_for, abort
from mongoengine import DoesNotExist

from forms import *
from flask_wtf.csrf import CSRFProtect
from models import db, Users, Operators, Devices, Userdevices, Info, Admins
from werkzeug.security import generate_password_hash
from flask_login import current_user, login_user, login_required, logout_user, LoginManager
import auth
import random
import csv
import uuid
from clickhouse_driver import Client
import logging
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer, SignatureExpired

app = Flask(__name__)

# Initializing logger
gunicorn_error_logger = logging.getLogger('gunicorn.error')
app.logger.handlers.extend(gunicorn_error_logger.handlers)
app.logger.setLevel(gunicorn_error_logger.level)

# Loading configuration
app.config.from_object('default_settings')
config_loaded = app.config.from_envvar('FLASK_CONFIG', silent=True)
if not config_loaded:
    app.logger.warning("Default config was loaded. "
                       "Change FLASK_CONFIG value to absolute path of your config file for correct loading.")

db.init_app(app)  # Init mongoengine
manager = LoginManager(app)  # Init login manager
csrf = CSRFProtect(app)  # Init CSRF in WTForms for excluding it in interaction with phone (well...)
url_tokenizer = URLSafeTimedSerializer(
    app.config['SECRET_KEY'])  # Init serializer for generating email confirmation tokens
mail = Mail(app)  # For sending confirmation emails
clckhs_client = Client(host='clickhouse', password=app.config['CLICKHOUSE_PASS'])  # ClickHouse config


def create_file(user_id, device_id, begin, end):
    """Generates file with data"""
    name = user_id + '_' + device_id.replace(':', '')
    query = "select * from {} where Clitime between '{}' and '{}'".format(name, begin, end)
    res = clckhs_client.execute(query)
    file_name = name + '_' + str(random.randint(1, 1000000)) + '.csv'

    d = Userdevices.objects(user_id=user_id).first()
    d_type = d.device_type

    device = Devices.objects(device_type=d_type).first()
    columns = device.columns.split(',')

    with open('files/' + file_name, 'w+') as out:
        csv_out = csv.writer(out)
        csv_out.writerow(columns)
        for row in res:
            csv_out.writerow(row)

    return file_name


@manager.user_loader
def load_user(user_id):
    """Configure user loader"""
    return Operators.objects(pk=user_id).first()

@app.route('/auth/', methods=['POST'])
@csrf.exempt
def authenticate():
    data = request.get_json()
    if data['login'] and data['password']:
        confirmed, jwt, code, user_id = auth.check_user(data['login'], data['password'])
        return jsonify({'jwt':jwt, "confirmed": confirmed, "user_id":user_id}), code
    return jsonify({}), 403

@app.route('/')
def main():
    """Index page"""
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    else:
        return redirect(url_for('get_data'))


@app.route('/login/', methods=['GET', 'POST'])
def login():
    """Login page"""
    form = LoginForm()
    if request.method == 'POST':
        operator = Operators.objects(login=form.username.data).first()
        if operator and operator.password_valid(form.password.data):
            login_user(operator)
            return redirect(url_for('main'))
        form.validate_on_submit()
        if not operator:
            form.username.errors.append("Пользователь не зарегистрирован")
        elif not operator.password_valid(form.password.data):
            form.password.errors.append("Неверное имя или пароль")
    return render_template("login.html", form=form)


@app.route('/admins/', methods=['GET'])
@login_required
def admin_panel():
    if type(current_user._get_current_object()) is not Admins:
        abort(404)
    app.logger.info(f"Admin ({current_user.login}) come on admin panel")
    ops = Operators.objects
    device_types = Devices.objects
    app.logger.debug(f"Devices: {device_types}")
    return render_template("admin_panel.html", operators=ops, devices=device_types)


@app.route('/admins/add-operator/', methods=['GET', 'POST'])
@login_required
def admin_add_operator():
    form = AddOperator()
    if type(current_user._get_current_object()) is not Admins:
        abort(404)
    if form.validate_on_submit():
        app.logger.info(f"Admin ({current_user.login}) created operator {form.login.data}")
        op = Admins() if form.is_admin.data else Operators()
        op.login = form.login.data
        op.password = form.password.data
        op.save()
        return redirect(url_for('admins'))

    return render_template("add_operator.html", form=form)


@app.route('/admins/delete-operator/<string:login_for_del>', methods=['GET'])
@login_required
def admin_delete_operator(login_for_del):
    if type(current_user._get_current_object()) is not Admins:
        abort(404)
    app.logger.info(f"Admin ({current_user.login}) deleted user ...")
    try:
        ops = Operators.objects.get(login=login_for_del)  # TODO: same login problem
    except DoesNotExist:
        ops = Admins.objects.get_or_404(login=login_for_del)
    ops.delete()
    return redirect('/admins')


@app.route('/admins/add-device/', methods=['GET', 'POST'])
@login_required
def add_device_type():
    # TODO Implement
    return redirect('admin_panel')
    if type(current_user._get_current_object()) is not Admins:
        abort(404)
    form = AddDevice()
    return render_template("add_device.html", form=form)


@app.route('/admins/delete-device/<string:id_for_del>/', methods=['GET'])
@login_required
def delete_device_type(id_for_del):
    # TODO: implement device deletion
    return redirect(url_for('admin_panel'))


@app.route('/data/', methods=["POST", "GET"])
@login_required
def get_data():
    if request.method == 'POST':
        form = UserList()
        user_id = form.us_list.data
        form2 = UserData()
        devices = []
        session["user_id"] = user_id

        for d in Userdevices.objects(user_id=user_id):
            devices.append((d.device_id, d.device_name))

        form2.device.choices = devices
        return render_template('data2.html', form=form2)
    else:
        form = UserList()
        form.us_list.choices = [
            (u.user_id, "{} {} {}".format(u.name, u.surname, u.patronymic))
            for u in Info.objects
            if current_user.id in u.allowed
        ]

        return render_template('data.html', form=form)


@app.route('/data/next/', methods=["POST", "GET"])
@login_required
def get_data_second():
    form = UserData()
    device = form.device.data
    app.logger.info(device)
    date_begin = form.date_begin.data
    date_end = form.date_end.data

    file = create_file(session['user_id'], device, date_begin, date_end)
    return render_template('upload_file.html', name=session['user_id'], file=file)


@app.route('/users/', methods=["POST", "GET"])
@login_required
def user_info():
    if request.method == 'GET':
        form = UserList()
        form.us_list.choices = [
            (u.user_id, "{} {} {}".format(u.name, u.surname, u.patronymic))
            for u in Info.objects
            if current_user.id in u.allowed
        ]
        return render_template('user_info.html', form=form)
    else:
        form = UserList()
        d = {}
        q = Info.objects(user_id=form.us_list.data).first()
        for i in q:
            d[i] = q[i]
        return render_template('user_info_data.html', user=d)


@app.route('/devices/', methods=["POST", "GET"])
@login_required
def devices():
    objects = Devices.objects()
    devices = {}
    for item in objects:
        if not item.device in devices:
            devices[item.device] = []
        devices[item.device].append([])
    return render_template('devices.html', devices=devices)


@app.route('/download/<file>')
@login_required
def download_file(file):
    return send_file('files/' + file, as_attachment=True)


@app.route('/users/register/', methods=['POST'])
@csrf.exempt
def new_user():
    data = request.get_json()
    man_info = Info.objects(email=data['email']).first()
    if man_info:
        man_user = Users.objects(user_id=man_info.user_id).first()
        if man_user.confirmed:
            return {"error": "email"}, 200
        else:
            man_info.delete()
            man_user.delete()
    if Users.objects(login=data['login']).first():
        return {"error": "login"}, 200
    id = uuid.uuid4().hex
    usr = Users()
    usr.user_id = id
    usr.login = data['login']
    usr.password_hash = generate_password_hash(data['password'])
    usr.confirmed = False
    usr.save()

    info = Info()
    info.user_id = id
    info.email = data['email']
    info.name = data['name']
    info.surname = data['surname']
    info.patronymic = data['patronymic']
    info.birth_date = data['birthdate']
    info.phone = data['phone_number']
    info.weight = 0
    info.height = 0
    info.save()

    token = url_tokenizer.dumps(data['email'], salt='email-confirm')
    msg = Message('Confirm Email', sender='iomt.confirmation@gmail.com', recipients=[data['email']])
    link = url_for('confirm_email', user_id=id, token=token, _external=True)
    msg.body = 'Your link is {}'.format(link)
    mail.send(msg)
    return {"error": ""}, 200


@app.route('/confirm_email/<user_id>/<token>')
def confirm_email(user_id, token):
    try:
        url_tokenizer.loads(token, salt='email-confirm', max_age=3600)
    except SignatureExpired:
        return '<h1>The link is expired!</h1>'
    user = Users.objects(user_id=user_id).first()
    user.confirmed = True
    user.save()
    return '<h1>Email confirmed!</h1>'


@app.route('/users/info/', methods=['GET', 'POST'])
@csrf.exempt
def get_info():
    token = request.args.get('token')
    user_id = request.args.get('user_id')
    if not token or not user_id or not auth.check_token(token):
        return {}, 403
    if request.method == 'GET':
        info = Info.objects(user_id=user_id).first()
        weight = 0 if not info.weight else info.weight
        height = 0 if not info.height else info.height
        return {"weight": weight, "height": height, "name": info.name, "surname": info.surname,
                "patronymic": info.patronymic, "email": info.email, "birthdate": info.birth_date,
                "phone_number": info.phone}, 200
    else:
        data = request.get_json()
        info = Info.objects(user_id=user_id).first()
        info.weight = data['weight']
        info.height = data['height']
        info.email = data['email']
        info.name = data['name']
        info.surname = data['surname']
        info.patronymic = data['patronymic']
        info.birth_date = data['birthdate']
        info.phone = data['phone_number']
        info.save()
        return {}, 200


@app.route('/users/allow/', methods=["POST"])
@csrf.exempt
def allow_operator():
    """Allow access for concrete operator"""
    token = request.args.get('token')
    user_id = request.args.get('user_id')
    op_id = request.args.get('operator_id')
    if not token or not user_id or not auth.check_token(token):
        return {}, 403
    op = Operators.objects.get_or_404(id=op_id)
    Info.objects(user_id=user_id).update_one(add_to_set__allowed=op.id)
    return {}, 200


@app.route('/devices/register/', methods=['POST'])
@csrf.exempt
def register_device():
    token = request.args.get('token')
    user_id = request.args.get('user_id')  # FIXME: Дыра, любой зареганый пользователь может зарегать на другого девайс
    if not token or not user_id or not auth.check_token(token):
        return {}, 403
    data = request.get_json()
    device = Userdevices()
    device.user_id = user_id
    device.device_id = data['device_id']
    device.device_name = data['device_name']
    device.device_type = data['device_type']
    device.save()

    table_name = user_id + '_' + data['device_id'].replace(':', '')
    app.logger.info("TABLE %s", table_name)
    obj = Devices.objects(device_type=data['device_type']).first()
    if not obj:
        return {}, 403

    create_str = obj.create_str.format(table_name)
    app.logger.info("CREATE %s", create_str)

    clckhs_client.execute(create_str)
    return {}, 200


@app.route('/devices/get/', methods=['GET'])
def get_user_devices():
    token = request.args.get('token')
    user_id = request.args.get('user_id')
    if not token or not user_id or not auth.check_token(token):
        return {}, 403
    objects = Userdevices.objects(user_id=user_id)
    user_devices = [
        {"device_id": obj.device_id, "device_name": obj.device_name, "device_type": obj.device_type}
        for obj in objects
    ]
    return jsonify({"devices": user_devices}), 200


@app.route('/devices/types/', methods=['GET'])
def get_devices():
    token = request.args.get('token')
    user_id = request.args.get('user_id')
    if not token or not user_id or not auth.check_token(token):
        return {}, 403
    devices_types = [
        {"device_type": obj.device_type, "prefix": obj.prefix}
        for obj in Devices.objects()
    ]
    for obj in Devices.objects():
        devices_types.append({"device_type": obj.device_type, "prefix": obj.prefix})
    return jsonify({"devices": devices_types}), 200


@app.route('/devices/delete/', methods=['GET'])
def delete_device():
    token = request.args.get('token')
    user_id = request.args.get('user_id')
    device_id = request.args.get('id')
    if not token or not user_id or not auth.check_token(token):
        return {}, 403
    d = Userdevices.objects(user_id=user_id, device_id=device_id).first()
    d.delete()
    return {}, 200


@app.route('/jwt/', methods=['GET'])
def cjwt():
    token = request.args.get('token')
    if not token or not auth.check_token(token):
        return jsonify({"valid": False})
    else:
        return jsonify({"valid": True})


@app.route('/logout/')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
