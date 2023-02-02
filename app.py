from random import randint
from flask import Flask, render_template, redirect, url_for, request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, current_user, logout_user, login_required
import smartsheet
import requests
import config
import re
from datetime import datetime
from utils import hash_pass, verify_pass

SMARTSHEET_TOKEN = config.smartsheet_token

ss_client = smartsheet.Smartsheet(SMARTSHEET_TOKEN)

carriers = config.carriers
driver_id_to_scac = config.driver_id_to_scac
locations = config.locations

OPEN_MOVES_LOG_SHEETS = config.open_move_log_sheet_id

open_moves_log_version = ''
open_moves_log = []

col_names = {}
col_id_filter = config.col_id_filter

driver_page_sessions = {}  # {session_id: timestamp}


class WorkflowMove:
    def __init__(self, move_id, row_id, container_number, load_status, priority, customer, origin, destination, scac,
                 truck_number, driver_id, ss_status, comments):
        self.move_id = move_id
        self.row_id = row_id
        self.container_number = container_number
        self.load_status = load_status
        self.priority = priority
        self.customer = customer
        self.origin = origin
        self.destination = destination
        self.scac = scac
        self.truck_number = truck_number
        self.driver_id = driver_id
        self.ss_status = ss_status
        self.comments = comments

    def __repr__(self):
        return self.move_id

    def add_scac(self, scac):
        if scac is None:
            self.move_id += scac
            self.scac = scac


workflow = {}  # format as following:
# {'Unique Move ID': WorkflowMove}

app = Flask(__name__,
            static_folder='static', )

app.config['SECRET_KEY'] = config.app_secret_key
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///database.db'
app.config['SQLALCHEMY_BINDS'] = {
    'log': 'sqlite:///logs.db',
    'users': 'sqlite:///users.db',
    'completed_moves': 'sqlite:///completed_moves.db'
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)


# database model to authenticate user
class User(db.Model, UserMixin):
    __bind_key__ = 'users'
    __tablename__ = 'user'

    """
    id          : an id generated upon registration.
    username    : an email or some other login to authorise user.
    password    : a password of a user.
    type        : user type to define who the hell is he.
    location    : to define where is the user authorised from
    scac        : scac if dispatch.
    
    user types:
    supervisor - can do anything.
    wc_admin - admin, can do anything with drivers and etc. Has the ability to edit passwords, create new user.
    gate_operator - a user with permission to check in/out drivers.
    [WIP] dispatch - dispatch, an automated report.
    """

    id = db.Column(db.Integer, primary_key=True, unique=True, autoincrement=True)
    alternative_id = db.Column(db.Integer, unique=True, nullable=True)
    email = db.Column(db.String(256), unique=True)
    password = db.Column(db.LargeBinary)
    type = db.Column(db.String(16))
    location = db.Column(db.String(128), nullable=True)
    scac = db.Column(db.String(8), nullable=True)
    is_suspended = db.Column(db.Boolean)
    created_at = db.Column(db.DateTime, default=db.func.localtimestamp())
    modified_at = db.Column(db.DateTime, default=db.func.localtimestamp(), onupdate=db.func.localtimestamp())

    def __init__(self, alternative_id, email, password, user_type, location, is_suspended):
        self.alternative_id = alternative_id
        self.email = email
        self.password = password
        self.type = user_type
        self.location = location
        self.is_suspended = is_suspended
        self.scac = None

    def get_id(self):
        return str(self.alternative_id)

    def get_type(self):
        return self.type

    def get_location(self):
        return self.location

    def get_real_id(self):
        return self.id


class InviteCodes(db.Model):
    __tablename__ = 'invite_codes'

    id = db.Column(db.Integer, primary_key=True, unique=True, autoincrement=True)
    email = db.Column(db.String(256), unique=True)
    invite_code = db.Column(db.String(7))
    user_type = db.Column(db.String(16))
    created_at = db.Column(db.DateTime, default=db.func.localtimestamp())
    modified_at = db.Column(db.DateTime, default=db.func.localtimestamp(), onupdate=db.func.localtimestamp())

    def __init__(self, email, invite_code, user_type):
        self.email = email
        self.invite_code = invite_code
        self.user_type = user_type


class OpenMoves(db.Model):
    __tablename__ = 'open_moves'
    """

    :param move_id: nullable
    :param row_id: nullable
    :param container_number: nullable
    :param load_status: 'Full'/'Empty'/'Bobtail'
    :param priority: 'HP'/'2P'/'ST'
    :param customer: customer to match with drivers before assignment
    :param origin: from
    :param destination: to, nullable
    :param ss_status: open/completed/issue/utl/otw, nullable
    :param driver_id: shuttle-id
    :param status: a more detailed status of a move to reflect what's actually going on: 'PENDING_DRIVER_ARRIVAL'/'SEARCHING'/'FOUND'/'OTW'/'DROPPING_OFF'/'DELIVERED'/'ISSUE'/'DAMAGED'/'ISSUE_OTW'
    :param truck_number: Number of a truck assigned to the move
    :param truck_license_plate: License plate of a truck assigned to the move
    :var self.pic_origin: picture id before pickup, nullable
    :var self.pic_destination: picture id at drop-off, nullable
    """

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    move_id = db.Column(db.String(24), nullable=True)
    row_id = db.Column(db.BigInteger, nullable=True)
    container_number = db.Column(db.String(32), nullable=True)
    load_status = db.Column(db.String(8))
    priority = db.Column(db.String(8))
    customer = db.Column(db.String(64))
    origin = db.Column(db.String(128))
    destination = db.Column(db.String(128))
    scac = db.Column(db.String(8))
    ss_status = db.Column(db.String(10), nullable=True)
    driver_id = db.Column(db.String(16))
    status = db.Column(db.String(32))
    truck_number = db.Column(db.String(8))
    truck_license_plate = db.Column(db.String(8))
    pic_origin = db.Column(db.String(32), nullable=True)  # TODO: save photo locally
    pic_destination = db.Column(db.String(32), nullable=True)  # TODO: save photo locally
    created_at = db.Column(db.DateTime, default=db.func.localtimestamp())
    modified_at = db.Column(db.DateTime, default=db.func.localtimestamp(), onupdate=db.func.localtimestamp())

    def __init__(self, move_id, row_id, container_number, load_status, priority, customer, origin, destination, scac,
                 ss_status, driver_id, status, truck_number, truck_license_plate):
        """

        :param move_id: a move id to reference in SS
        :param row_id: a row ID of a move for fast access and updates
        :param container_number: A container number, if exists
        :param load_status: A kind of move within the following 3: 'Full'/'Empty'/'Bobtail'
        :param priority: 'HP'/'2P'/'ST'
        :param customer: customer to bill within the following move
        :param origin: origin
        :param destination: destination
        :param scac: SCAC of the CARRIER the driver belongs to
        :param ss_status: a status to reference within SS: 'open'/'completed'/'issue'/'utl'/'otw'
        :param driver_id: a shuttle-ID to reference a driver
        :param status: a more detailed status of a move to reflect what's actually going on: 'PENDING_DRIVER_ARRIVAL'/'SEARCHING'/'FOUND'/'OTW'/'DROPPING_OFF'/'DELIVERED'/'ISSUE'/'DAMAGED'/'ISSUE_OTW'
        :param truck_number: Number of a truck assigned to the move
        :param truck_license_plate: License plate of a truck assigned to the move
        """
        self.move_id = move_id
        self.row_id = row_id
        self.container_number = container_number
        self.load_status = load_status
        self.priority = priority
        self.customer = customer
        self.origin = origin
        self.destination = destination
        self.scac = scac
        self.ss_status = ss_status
        self.driver_id = driver_id
        self.status = status
        self.truck_number = truck_number
        self.truck_license_plate = truck_license_plate
        self.pic_origin = None
        self.pic_destination = None
        pass

    def __repr__(self):
        return str(self.move_id)


class CompletedMoves(db.Model):
    __bind_key__ = 'completed_moves'
    __tablename__ = 'completed_moves'
    """

    :param move_id: nullable
    :param row_id: nullable
    :param container_number: nullable
    :param load_status: 'Full'/'Empty'/'Bobtail'
    :param priority: 'HP'/'2P'/'ST'
    :param customer: customer to match with drivers before assignment
    :param origin: from
    :param destination: to, nullable
    :param ss_status: open/completed/issue/utl/otw, nullable
    :param driver_id: shuttle-id
    :param status: a more detailed status of a move to reflect what's actually going on: 'PENDING_DRIVER_ARRIVAL'/'SEARCHING'/'FOUND'/'OTW'/'DROPPING_OFF'/'DELIVERED'/'ISSUE'/'DAMAGED'/'ISSUE_OTW'
    :param truck_number: Number of a truck assigned to the move
    :param truck_license_plate: License plate of a truck assigned to the move
    :var self.pic_origin: picture id before pickup, nullable
    :var self.pic_destination: picture id at drop-off, nullable
    """

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    move_id = db.Column(db.String(24), nullable=True)
    row_id = db.Column(db.BigInteger, nullable=True)
    container_number = db.Column(db.String(32), nullable=True)
    load_status = db.Column(db.String(8))
    priority = db.Column(db.String(8))
    customer = db.Column(db.String(64))
    origin = db.Column(db.String(128))
    destination = db.Column(db.String(128))
    scac = db.Column(db.String(8))
    ss_status = db.Column(db.String(10), nullable=True)
    driver_id = db.Column(db.String(16))
    status = db.Column(db.String(32))
    truck_number = db.Column(db.String(8))
    truck_license_plate = db.Column(db.String(8))
    pic_origin = db.Column(db.String(32), nullable=True)  # TODO: save photo locally
    pic_destination = db.Column(db.String(32), nullable=True)  # TODO: save photo locally
    created_at = db.Column(db.DateTime, default=db.func.localtimestamp())
    modified_at = db.Column(db.DateTime, default=db.func.localtimestamp(), onupdate=db.func.localtimestamp())

    def __init__(self, move_id, row_id, container_number, load_status, priority, customer, origin, destination, scac,
                 ss_status, driver_id, status, truck_number, truck_license_plate):
        """

        :param move_id: a move id to reference in SS
        :param row_id: a row ID of a move for fast access and updates
        :param container_number: A container number, if exists
        :param load_status: A kind of move within the following 3: 'Full'/'Empty'/'Bobtail'
        :param priority: 'HP'/'2P'/'ST'
        :param customer: customer to bill within the following move
        :param origin: origin
        :param destination: destination
        :param scac: SCAC of the CARRIER the driver belongs to
        :param ss_status: a status to reference within SS: 'open'/'completed'/'issue'/'utl'/'otw'
        :param driver_id: a shuttle-ID to reference a driver
        :param status: a more detailed status of a move to reflect what's actually going on: 'PENDING_DRIVER_ARRIVAL'/'SEARCHING'/'FOUND'/'OTW'/'DROPPING_OFF'/'DELIVERED'/'ISSUE'/'DAMAGED'/'ISSUE_OTW'
        :param truck_number: Number of a truck assigned to the move
        :param truck_license_plate: License plate of a truck assigned to the move
        """
        self.move_id = move_id
        self.row_id = row_id
        self.container_number = container_number
        self.load_status = load_status
        self.priority = priority
        self.customer = customer
        self.origin = origin
        self.destination = destination
        self.scac = scac
        self.ss_status = ss_status
        self.driver_id = driver_id
        self.status = status
        self.truck_number = truck_number
        self.truck_license_plate = truck_license_plate
        self.pic_origin = None
        self.pic_destination = None

    def __repr__(self):
        return str(self.move_id)


class SiteLog(db.Model):
    __bind_key__ = 'log'
    __tablename__ = 'website_log'

    id = db.Column(db.Integer, primary_key=True, unique=True, autoincrement=True)
    action_type = db.Column(db.String(64))
    by_user = db.Column(db.String(256))
    affected_user = db.Column(db.Integer, nullable=True)
    detailed_info = db.Column(db.String(2048), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.localtimestamp())
    modified_at = db.Column(db.DateTime, default=db.func.localtimestamp(), onupdate=db.func.localtimestamp())

    def __init__(self, action_type, by_user, affected_user, detailed_info):
        self.action_type = action_type
        self.by_user = by_user
        self.affected_user = affected_user
        self.detailed_info = detailed_info


class MoveLog(db.Model):
    __bind_key__ = 'log'
    __tablename__ = 'move_log'

    id = db.Column(db.Integer, primary_key=True, unique=True, autoincrement=True)
    action_type = db.Column(db.String(64))
    by_user = db.Column(db.String(256), nullable=True)
    driver_id = db.Column(db.String(16), nullable=True)
    scac = db.Column(db.String(8), nullable=True)
    move_id = db.Column(db.String(24), nullable=True)
    detailed_info = db.Column(db.String(2048), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.localtimestamp())
    modified_at = db.Column(db.DateTime, default=db.func.localtimestamp(), onupdate=db.func.localtimestamp())

    def __init__(self, action_type, by_user, driver_id, scac, move_id, detailed_info):
        self.action_type = action_type
        self.by_user = by_user
        self.driver_id = driver_id
        self.scac = scac
        self.move_id = move_id
        self.detailed_info = detailed_info


with app.app_context():
    db.create_all()

login_manager = LoginManager(app)
login_manager.login_view = 'login'


def new_alt_id():
    while True:
        new_random_id = randint(1, 200000000)
        user = User.query.filter_by(alternative_id=new_random_id).first()
        if user is None:
            return new_random_id


def new_invite_token():
    import string
    import secrets
    while True:
        alphabet = string.ascii_uppercase + string.digits
        new_random_invite_token = ''.join(secrets.choice(alphabet) for i in range(7))
        invite_code = InviteCodes.query.filter_by(invite_code=new_random_invite_token).first()
        if invite_code is None:
            return new_random_invite_token


def new_site_log(action_type, by_user, affected_user=None, detailed_info=None):
    new_log = SiteLog(action_type, by_user, affected_user, detailed_info)
    db.session.add(new_log)
    db.session.commit()


def new_move_log(action_type, by_user, driver_id, scac, move_id, detailed_info=None):
    """
    :param action_type: ASSIGN/UNASSIGN/DELETE/GATE_IN/GATE_OUT/PICTURE_UPLOAD/COMPLETED/ASSIGNED_FROM_NEXT/ISSUE/
    :param by_user: email of user committing action, if action by server will display SERVER
    :param driver_id: drivers SHUTTLE-ID associated with the move
    :param scac: SCAC code of company the driver belongs to
    :param move_id: Move ID action is taken on
    :param detailed_info: extra info if applicable
    """
    new_log = MoveLog(action_type, by_user, driver_id, scac, move_id, detailed_info)
    db.session.add(new_log)
    db.session.commit()


@login_manager.user_loader
def load_user(user_id):
    user = User.query.filter_by(alternative_id=user_id).first()
    if user is None or user.is_suspended:
        return None
    else:
        return user


def update_workflow_list(forced=False):
    """
    Check if workflow was updated.
    If so then sync current workflow variable with SS open move log.

    :param forced: True to force sync of current workflow
    """
    new_version = ss_client.Sheets.get_sheet_version(OPEN_MOVES_LOG_SHEETS).__str__()

    global open_moves_log_version
    if not forced and open_moves_log_version == new_version:
        return

    open_moves_log_version = new_version

    global open_moves_log
    open_moves_log = ss_client.Sheets.get_sheet(OPEN_MOVES_LOG_SHEETS, column_ids=col_id_filter)

    global workflow
    workflow = {}

    for i in open_moves_log.rows:
        workflow.update({i.cells[0].display_value: WorkflowMove(i.cells[0].display_value,
                                                                i.id,
                                                                i.cells[1].display_value,
                                                                i.cells[2].display_value,
                                                                i.cells[3].display_value,
                                                                i.cells[4].display_value,
                                                                i.cells[5].display_value,
                                                                i.cells[6].display_value,
                                                                i.cells[7].display_value,
                                                                i.cells[8].display_value,
                                                                i.cells[9].display_value,
                                                                i.cells[10].display_value,
                                                                i.cells[11].display_value)})


update_workflow_list(forced=True)


@app.route('/')
@login_required
def index():
    return render_template('home/index.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return render_template('accounts/register.html')

    new_user_email = request.form['email']
    if hasattr(new_user_email, '__iter__') and not isinstance(new_user_email, str):
        new_user_email = new_user_email[0]

    invited_user = InviteCodes.query.filter_by(email=new_user_email).first()
    if invited_user is None:
        return render_template('accounts/register.html',
                               message='Email not invited')

    new_user_invite_code = request.form['invite_code']
    if hasattr(new_user_invite_code, '__iter__') and not isinstance(new_user_invite_code, str):
        new_user_invite_code = new_user_invite_code[0]

    if new_user_invite_code.upper() != invited_user.invite_code:
        return render_template('accounts/register.html',
                               message="Invite code doesn't match")

    new_user_password = request.form['password']
    if hasattr(new_user_password, '__iter__') and not isinstance(new_user_password, str):
        new_user_password = new_user_password[0]

    user = User.query.filter_by(email=new_user_email).first()
    if user is None:
        new_user = User(new_alt_id(), new_user_email, hash_pass(new_user_password), invited_user.user_type, None, False)

        db.session.add(new_user)
        db.session.delete(invited_user)
        db.session.commit()
        new_site_log('REGISTERED', new_user_email)

        return redirect(url_for('login', message='Successfully registered, please login'))

    else:
        user.password = hash_pass(new_user_password)
        user.alternative_id = new_alt_id()
        db.session.delete(invited_user)
        db.session.commit()
        new_site_log('PASSWORD_CHANGE', new_user_email)

        return redirect(url_for('login', message='Password Updated'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'GET':
        return render_template('accounts/login.html', message=request.args.get('message'))

    provided_email = request.form['email']
    if hasattr(provided_email, '__iter__') and not isinstance(provided_email, str):
        provided_email = provided_email[0]

    provided_password = request.form['password']
    if hasattr(provided_password, '__iter__') and not isinstance(provided_password, str):
        provided_password = provided_password[0]

    user = User.query.filter_by(email=provided_email).first()

    if user is None:
        return render_template('accounts/login.html',
                               message="User doesn't exist")

    if verify_pass(provided_password, user.password):
        login_user(user)
        new_site_log('LOG_IN', user.email)
        return redirect(url_for('index'), 302)

    else:
        return render_template('accounts/login.html',
                               message="Wrong password")


@app.route('/logout', methods=['GET', 'POST'])
@login_required
def logout():
    new_site_log('LOG_OUT', current_user.email)
    logout_user()
    return redirect(url_for('login'), 302)


@app.route('/invite_codes', methods=['GET', 'POST'])
@login_required
def invite_codes():
    user = User.query.filter_by(id=int(current_user.get_real_id())).first()

    if user.type != 'supervisor':
        return redirect(url_for('index'), 302)

    if request.method == 'GET':
        user_types = ['supervisor', 'wc_admin', 'gate_operator', 'dispatch']

        invites = InviteCodes.query.all()
        existing_invites = []

        for invite in invites:
            existing_invites.append([invite.email, invite.invite_code, invite.user_type])

        return render_template('home/invite_codes.html',
                               user_types=user_types,
                               existing_invites=existing_invites,
                               message=request.args.get('message'))

    if 'invite_code_to_delete' in request.form:
        invite_to_delete = request.form['invite_code_to_delete']
        if hasattr(invite_to_delete, '__iter__') and not isinstance(invite_to_delete, str):
            invite_to_delete = invite_to_delete[0]

        invite_to_delete = InviteCodes.query.filter_by(invite_code=invite_to_delete).first()
        if invite_to_delete:
            new_site_log('INVITE_CODE_DELETED', current_user.email, affected_user=invite_to_delete.email)
            db.session.delete(invite_to_delete)
            db.session.commit()

        return redirect(url_for('invite_codes'), 302)

    new_invite_email = request.form['email']
    if hasattr(new_invite_email, '__iter__') and not isinstance(new_invite_email, str):
        new_invite_email = new_invite_email[0]

    new_invite_user_type = request.form['user_type']
    if hasattr(new_invite_user_type, '__iter__') and not isinstance(new_invite_user_type, str):
        new_invite_user_type = new_invite_user_type[0]

    invite = InviteCodes.query.filter_by(email=new_invite_email).first()
    if invite is None:
        new_invite = InviteCodes(new_invite_email, new_invite_token(), new_invite_user_type)
        db.session.add(new_invite)
        db.session.commit()
        new_site_log('INVITE_CODE_GENERATED', current_user.email, affected_user=new_invite_email)
        return redirect(url_for('invite_codes', message=f"user invited, give them the invite code pls"), 302)
    else:
        invite.invite_code = new_invite_token()
        invite.user_type = new_invite_user_type
        db.session.commit()
        new_site_log('INVITE_CODE_UPDATED', current_user.email, affected_user=new_invite_email)
        return redirect(url_for('invite_codes', message=f"invite code updated"), 302)


@app.route('/profile', methods=['POST', 'GET'])
@login_required
def profile():
    user = User.query.filter_by(id=int(current_user.get_real_id())).first()

    if request.method == 'GET':
        return render_template('home/profile.html',
                               user_type=user.type,
                               password_message=request.args.get('password_message') or '')

    if request.form['change_password']:
        current_password = request.form['current_password']
        if hasattr(current_password, '__iter__') and not isinstance(current_password, str):
            current_password = current_password[0]

        if not verify_pass(current_password, user.password):
            return redirect(url_for('profile', password_message='Password is not correct.'), 302)

        new_password = request.form['new_password']
        if hasattr(new_password, '__iter__') and not isinstance(new_password, str):
            new_password = new_password[0]

        new_password_confirm = request.form['new_password_confirm']
        if hasattr(new_password_confirm, '__iter__') and not isinstance(new_password_confirm, str):
            new_password_confirm = new_password_confirm[0]

        if verify_pass(new_password, user.password):
            return redirect(url_for('profile', password_message='Password cannot be the same as the previous one.'),
                            302)

        if new_password != new_password_confirm:
            return redirect(url_for('profile', password_message="Passwords don't match."), 302)

        new_site_log('PASSWORD_UPDATED', current_user.email)
        user.password = hash_pass(new_password)
        user.alternative_id = new_alt_id()
        db.session.commit()
        logout_user()
        return redirect(url_for('login', message="Password changed, please login."), 302)

    return redirect(url_for('profile'), 302)


@app.route('/users', methods=['GET', 'POST'])
@login_required
def users():
    user = User.query.filter_by(id=int(current_user.get_real_id())).first()

    if user.type != 'supervisor':
        return redirect(url_for('index'), 302)

    if request.method == 'GET':
        existing_users_db = User.query.all()
        existing_users = []

        for existing_user in existing_users_db:
            existing_users.append(
                [existing_user.email, existing_user.type, existing_user.is_suspended, existing_user.get_id()])

        return render_template('home/users.html', existing_users=existing_users)

    return redirect(url_for('users'), 302)


@app.route('/user_manager/<alternative_id>', methods=['GET', 'POST'])
@login_required
def user_manager(alternative_id):
    user = User.query.filter_by(id=int(current_user.get_real_id())).first()

    # if user.type != 'supervisor':
    #    return redirect(url_for('users'), 302)

    user_to_update = User.query.filter_by(alternative_id=int(alternative_id)).first()
    if user_to_update is None:
        return redirect(url_for('users'), 302)

    if user.get_id() == user_to_update.get_id():
        pass  # return redirect(url_for('users'), 302)

    user_types = ['supervisor', 'wc_admin', 'gate_operator', 'dispatch']

    if request.method == 'GET':
        scac_list = ['None'] + list(carriers.keys())

        return render_template('home/user_manager.html',
                               locations=locations,
                               scac_list=scac_list,
                               object_alternative_id=alternative_id,
                               user_types=user_types,
                               object_email=user_to_update.email,
                               object_user_type=user_to_update.type,
                               object_is_suspended=user_to_update.is_suspended,
                               object_carrier=user_to_update.scac,
                               object_location=user_to_update.location)

    if 'new_location' in request.form.keys() and user_to_update.type != 'dispatch':
        new_site_log('LOCATION_UPDATE', current_user.email, affected_user=user_to_update.email, detailed_info=f'Old loc: {str(user_to_update.location)} | New loc: {request.form["new_location"]}')
        user_to_update.location = request.form['new_location']
        db.session.commit()
        return redirect(url_for(f'user_manager', alternative_id=alternative_id), 302)

    if 'new_scac' in request.form.keys() and user_to_update.type == 'dispatch':
        new_site_log('SCAC_UPDATE', current_user.email, affected_user=user_to_update.email, detailed_info=f'Old scac: {str(user_to_update.scac)} | New scac: {request.form["new_scac"]}')
        user_to_update.scac = request.form['new_scac']
        db.session.commit()
        return redirect(url_for(f'user_manager', alternative_id=alternative_id), 302)

    if 'new_user_type' in request.form.keys():
        new_site_log('USER_TYPE_UPDATE', current_user.email, affected_user=user_to_update.email, detailed_info=f'Old user type: {str(user_to_update.type)} | New user type: {request.form["new_user_type"]}')
        user_to_update.scac = None
        user_to_update.location = None
        user_to_update.type = request.form['new_user_type']
        db.session.commit()
        return redirect(url_for(f'user_manager', alternative_id=alternative_id), 302)

    if 'suspend_user' in request.form.keys():
        user_to_update.is_suspended = True
        db.session.commit()
        new_site_log('USER_SUSPENDED', current_user.email, affected_user=user_to_update.email)
        return redirect(url_for(f'user_manager', alternative_id=alternative_id), 302)

    if 'un_suspend_user' in request.form.keys():
        user_to_update.is_suspended = False
        db.session.commit()
        new_site_log('USER_UN_SUSPENDED', current_user.email, affected_user=user_to_update.email)
        return redirect(url_for(f'user_manager', alternative_id=alternative_id), 302)

    if 'delete_user' in request.form.keys():
        new_site_log('USER_DELETED', current_user.email, affected_user=user_to_update.email)
        db.session.delete(user_to_update)
        db.session.commit()
        return redirect(url_for(f'user_manager', alternative_id=alternative_id), 302)

    return redirect(url_for(f'user_manager', alternative_id=alternative_id), 302)


@app.route('/driver/<raw_shuttle_id>', methods=['GET'])
@login_required
def driver_w_shuttle_id(raw_shuttle_id):
    if current_user.type == 'dispatch':
        return redirect(url_for('index'), 302)

    shuttle_id = str(raw_shuttle_id)

    if not re.fullmatch(r'[A-Z]{2}[\-][0-9]{4}', shuttle_id):
        return render_template('home/driver.html', message="Driver ID doesn't match format.")

    scac = driver_id_to_scac.get(shuttle_id[0:2], 'none')
    if not carriers.get(scac, False):
        return render_template('home/driver.html', message="Carrier not found")

    r = requests.get(f'{carriers.get(scac)}get_driver/{shuttle_id}')

    if r.status_code == 404:
        return render_template('home/driver.html', message="Driver not found")

    if r.status_code == 403:
        return render_template('home/driver.html', message="Driver is suspended of banned")

    if r.status_code == 204:
        return render_template('home/driver.html', message="Driver is not set as working today by dispatch")

    if r.status_code != 200:
        # TODO: log the error
        return render_template('home/driver.html', message="Some strange error, please report this")

    update_workflow_list()

    r = r.json()

    assigned_customer = r.get('assigned_customer')
    driver_name = r.get('driver_name')
    truck_number = r.get('truck_number')
    license_plate = r.get('license_plate')

    authorised_from_location = current_user.location
    wc_admin = True if current_user.type == 'supervisor' or current_user.type == 'wc_admin' else False

    current_move_id = r.get('current_move_id')
    opposite_direction_move_id = r.get('next_move_id')

    current_move_msg = ''
    current_container_number, current_container_origin, current_container_destination = '', '', ''

    current_pending_bobtail = False
    opposite_direction_pending_bobtail = False

    move_ids_current_direction = ['STANDBY', 'BOBTAIL']
    move_ids_current_direction_no_scac = []

    move_ids_opposite_direction = ['BOBTAIL']
    move_ids_opposite_direction_no_scac = []

    current_move = OpenMoves.query.filter_by(driver_id=shuttle_id, move_id=current_move_id).first()
    if current_move is not None:
        current_container_number = current_move.container_number
        current_container_origin = current_move.origin
        current_container_destination = current_move.destination
        move = workflow.get(current_move_id, None)
        if move is None and current_move_id != 'BOBTAIL':
            current_move_msg = 'Move not found in open move log'

    for i in workflow.values():
        if i.customer == assigned_customer and not i.driver_id:
            if not current_move_id and i.origin == authorised_from_location:

                if i.scac == scac:
                    move_ids_current_direction.append(i.move_id)
                elif i.scac is None:
                    move_ids_current_direction_no_scac.append(i.move_id)

            elif not opposite_direction_move_id and i.origin == current_container_destination:

                if i.scac == scac:
                    move_ids_opposite_direction.append(i.move_id)
                elif i.scac is None:
                    move_ids_opposite_direction_no_scac.append(i.move_id)

    move_ids_current_direction.extend(move_ids_current_direction_no_scac)
    move_ids_opposite_direction.extend(move_ids_opposite_direction_no_scac)

    if request.args.get('current_pending_bobtail'):
        current_pending_bobtail = True

    if request.args.get('opposite_direction_pending_bobtail'):
        opposite_direction_pending_bobtail = True

    return render_template('home/driver.html',
                           full_request=True,
                           scac=scac,
                           driver_id=shuttle_id,
                           assigned_customer=assigned_customer,
                           driver_name=driver_name,
                           truck_number=truck_number,
                           license_plate=license_plate,

                           authorised_from_location=authorised_from_location,
                           wc_admin=wc_admin,
                           locations=locations,

                           move_ids_current_direction=move_ids_current_direction,
                           current_pending_bobtail=current_pending_bobtail,
                           current_move_id=current_move_id,
                           current_container_number=current_container_number,
                           current_container_origin=current_container_origin,
                           current_container_destination=current_container_destination,
                           current_move_msg=current_move_msg,

                           move_ids_opposite_direction=move_ids_opposite_direction,
                           opposite_direction_move_id=opposite_direction_move_id,
                           opposite_direction_pending_bobtail=opposite_direction_pending_bobtail
                           )


@app.route('/driver', methods=['GET', 'POST'])
@login_required
def driver():
    if current_user.type == 'dispatch':
        return redirect(url_for('index'), 302)

    if request.method == 'GET':
        return render_template('home/driver.html')

    scac = None
    shuttle_id = None

    if 'next_driver' in request.form:
        shuttle_id = request.form['next_driver']
        if hasattr(shuttle_id, '__iter__') and not isinstance(shuttle_id, str):
            shuttle_id = shuttle_id[0]

        return redirect(url_for('driver_w_shuttle_id', raw_shuttle_id=shuttle_id))

    else:
        shuttle_id = request.form['driver_id']
        if hasattr(shuttle_id, '__iter__') and not isinstance(shuttle_id, str):
            shuttle_id = shuttle_id[0]

        scac = request.form['scac']
        if hasattr(scac, '__iter__') and not isinstance(scac, str):
            scac = scac[0]

    r = requests.get(f'{carriers.get(scac)}get_driver/{shuttle_id}')

    if r.status_code == 404:
        return render_template('home/driver.html', message="Driver not found")

    if r.status_code == 403:
        return render_template('home/driver.html', message="Driver is suspended of banned")

    if r.status_code == 204:
        return render_template('home/driver.html', message="Driver is not set as working today by dispatch")

    if r.status_code != 200:
        # TODO: log the error
        return render_template('home/driver.html', message="Some strange error, please report this")

    wc_admin = True if current_user.type == 'supervisor' or current_user.type == 'wc_admin' else False

    update_workflow_list()

    r = r.json()
    truck_number = r.get('truck_number')
    truck_license_plate = r.get('license_plate')
    assigned_customer = r.get('assigned_customer')
    current_move_id = r.get('current_move_id')
    opposite_direction_move_id = r.get('next_move_id')

    if wc_admin and 'new_location' in request.form:
        new_possible_location = request.form['new_location']
        if hasattr(new_possible_location, '__iter__') and not isinstance(new_possible_location, str):
            new_possible_location = new_possible_location[0]

        if new_possible_location in locations:
            new_site_log('LOCATION_UPDATE', current_user.email, detailed_info=f'Old loc: {str(current_user.location)} | New loc: {new_possible_location}')
            current_user.location = new_possible_location
            db.session.commit()

        return redirect(url_for('driver_w_shuttle_id', raw_shuttle_id=shuttle_id))

    if 'new_current_move_id' in request.form:
        new_current_move_id = request.form['new_current_move_id']
        if hasattr(new_current_move_id, '__iter__') and not isinstance(new_current_move_id, str):
            new_current_move_id = new_current_move_id[0]

        if new_current_move_id == 'STANDBY':
            assign_current_move(scac, shuttle_id, 'STANDBY')

        elif new_current_move_id == 'BOBTAIL':
            return redirect(url_for('driver_w_shuttle_id', raw_shuttle_id=shuttle_id, current_pending_bobtail='True'))

        elif workflow.get(new_current_move_id, False):
            new_current_move = workflow.get(new_current_move_id)
            update_move_id_row(new_current_move.row_id,
                               new_scac=scac if new_current_move.scac is None else None,
                               new_driver_id=shuttle_id,
                               new_truck=truck_number,
                               new_status='Open')
            new_current_move.add_scac(scac)

            move = OpenMoves(new_current_move.move_id,
                             new_current_move.row_id,
                             new_current_move.container_number,
                             new_current_move.load_status,
                             new_current_move.priority,
                             new_current_move.customer,
                             new_current_move.origin,
                             new_current_move.destination,
                             scac,
                             'Open',
                             shuttle_id,
                             'SEARCHING',
                             truck_number,
                             truck_license_plate)

            db.session.add(move)
            db.session.commit()

            assign_current_move(scac, shuttle_id,
                                move_id=new_current_move.move_id,
                                origin=new_current_move.origin,
                                destination=new_current_move.destination,
                                container_number=new_current_move.container_number)

            new_move_log('ASSIGN', current_user.email, shuttle_id, scac, new_current_move_id)

        return redirect(url_for('driver_w_shuttle_id', raw_shuttle_id=shuttle_id))

    if 'new_opposite_direction_move_id' in request.form:
        new_opposite_direction_move_id = request.form['new_opposite_direction_move_id']
        if hasattr(new_opposite_direction_move_id, '__iter__') and not isinstance(new_opposite_direction_move_id, str):
            new_opposite_direction_move_id = new_opposite_direction_move_id[0]

        if new_opposite_direction_move_id == 'UNASSIGN':
            if opposite_direction_move_id != 'BOBTAIL' and workflow.get(opposite_direction_move_id, False):
                update_move_id_row(opposite_direction_move_id,
                                   new_scac=None,
                                   new_driver_id=None,
                                   new_truck=None,
                                   new_status=None)
                move = OpenMoves.query.filter_by(driver_id=shuttle_id, move_id=opposite_direction_move_id).first()
                if move:
                    db.session.delete(move)

            un_assign_next_move(scac, shuttle_id)
            new_move_log('UNASSIGN', current_user.email, shuttle_id, scac, opposite_direction_move_id, 'IS_NEXT')

        elif new_opposite_direction_move_id == 'BOBTAIL':
            return redirect(url_for('driver_w_shuttle_id', raw_shuttle_id=shuttle_id, opposite_direction_pending_bobtail='True'))

        elif workflow.get(new_opposite_direction_move_id, False):
            new_next_move = workflow.get(new_opposite_direction_move_id)
            update_move_id_row(new_next_move.row_id,
                               new_scac=scac if new_next_move.scac is None else None,
                               new_driver_id=shuttle_id,
                               new_truck=truck_number,
                               new_status='Open')
            new_next_move.add_scac(scac)

            move = OpenMoves(new_next_move.move_id,
                             new_next_move.row_id,
                             new_next_move.container_number,
                             new_next_move.load_status,
                             new_next_move.priority,
                             new_next_move.customer,
                             new_next_move.origin,
                             new_next_move.destination,
                             scac,
                             'Open',
                             shuttle_id,
                             'PENDING_DRIVER_ARRIVAL',
                             truck_number,
                             truck_license_plate)

            db.session.add(move)
            db.session.commit()

            assign_next_move(scac, shuttle_id, new_next_move.move_id)
            new_move_log('ASSIGN', current_user.email, shuttle_id, scac, opposite_direction_move_id, 'IS_NEXT')

        db.session.commit()
        return redirect(url_for('driver_w_shuttle_id', raw_shuttle_id=shuttle_id))

    if 'confirm_current_bobtail_destination' in request.form:
        if 'cancel_current_bobtail_destination' in request.form:
            return redirect(url_for('driver_w_shuttle_id', raw_shuttle_id=shuttle_id))

        confirm_current_bobtail_destination = request.form['confirm_current_bobtail_destination']
        if hasattr(confirm_current_bobtail_destination, '__iter__') and not isinstance(confirm_current_bobtail_destination, str):
            confirm_current_bobtail_destination = confirm_current_bobtail_destination[0]

        if confirm_current_bobtail_destination in locations and confirm_current_bobtail_destination != current_user.location:
            # TODO: assign move
            move = OpenMoves('BOBTAIL',
                             None,
                             None,
                             'Bobtail',
                             'ST',
                             assigned_customer,
                             current_user.location,
                             confirm_current_bobtail_destination,
                             scac,
                             'Open',
                             shuttle_id,
                             'SEARCHING',
                             truck_number,
                             truck_license_plate)

            db.session.add(move)
            db.session.commit()

            assign_current_move(scac, shuttle_id,
                                move_id='BOBTAIL',
                                origin=current_user.location,
                                destination=confirm_current_bobtail_destination,
                                container_number=None)
            new_move_log('ASSIGN', current_user.email, shuttle_id, scac, opposite_direction_move_id)

        return redirect(url_for('driver_w_shuttle_id', raw_shuttle_id=shuttle_id))

    if 'confirm_next_bobtail_destination' in request.form:
        if 'cancel_next_bobtail_destination' in request.form:
            return redirect(url_for('driver_w_shuttle_id', raw_shuttle_id=shuttle_id))

        confirm_next_bobtail_destination = request.form['confirm_next_bobtail_destination']

        if hasattr(confirm_next_bobtail_destination, '__iter__') and not isinstance(
                confirm_next_bobtail_destination, str):
            confirm_next_bobtail_destination = confirm_next_bobtail_destination[0]

        if confirm_next_bobtail_destination in locations and confirm_next_bobtail_destination != current_user.location:
            # TODO: assign move
            move = OpenMoves(None,
                             None,
                             'BOBTAIL',
                             'Bobtail',
                             'ST',
                             assigned_customer,
                             current_user.location,
                             confirm_next_bobtail_destination,
                             scac,
                             'Open',
                             shuttle_id,
                             'SEARCHING',
                             truck_number,
                             truck_license_plate)

            db.session.add(move)
            db.session.commit()

            assign_next_move(scac, shuttle_id, move_id='BOBTAIL')
            new_move_log('ASSIGN', current_user.email, shuttle_id, scac, opposite_direction_move_id, 'IS_NEXT')

        elif confirm_next_bobtail_destination == 'CANCEL_BOBTAIL':
            return redirect(url_for('driver_w_shuttle_id', raw_shuttle_id=shuttle_id))

        return redirect(url_for('driver_w_shuttle_id', raw_shuttle_id=shuttle_id))

    if 'update_status' in request.form:
        current_move = OpenMoves.query.filter_by(driver_id=shuttle_id, move_id=current_move_id).first()
        if current_move is None:
            return redirect(url_for('driver_w_shuttle_id', raw_shuttle_id=shuttle_id))

        move = workflow.get(current_move_id, None)
        current_move_msg = None
        if move is None and current_move_id != 'BOBTAIL':
            current_move_msg = 'Move not found in open move log'

        new_status = request.form['update_status']
        if hasattr(new_status, '__iter__') and not isinstance(new_status, str):
            new_status = new_status[0]

        if new_status == 'CONFIRM_CHECKOUT':
            update_move_id_row(current_move.row_id,
                               new_status='OTW',
                               new_update='Container Checked out')
            current_move.ss_status = 'OTW'
            current_move.status = 'OTW'
            new_move_log('GATE_OUT', current_user.email, shuttle_id, scac, current_move_id)

        elif new_status == 'ISSUE_DAMAGED':
            update_move_id_row(current_move.row_id,
                               new_status='Issue',
                               new_comment='Container Damaged')
            un_assign_current_move(scac, shuttle_id)
            db.session.delete(current_move)
            new_move_log('ISSUE', current_user.email, shuttle_id, scac, current_move_id)

        elif new_status == 'ISSUE_NOT_FOUND':
            update_move_id_row(current_move.row_id,
                               new_status='Issue',
                               new_comment='Container Not Found')
            un_assign_current_move(scac, shuttle_id)
            db.session.delete(current_move)
            new_move_log('ISSUE', current_user.email, shuttle_id, scac, current_move_id)

        elif new_status == 'UNASSIGN':
            update_move_id_row(current_move.row_id,
                               new_driver_id='',
                               new_truck='',
                               new_status='',
                               new_update='Container Has been unassigned')
            un_assign_current_move(scac, shuttle_id)
            db.session.delete(current_move)
            new_move_log('UNASSIGN', current_user.email, shuttle_id, scac, current_move_id)

        elif new_status == 'CONFIRM_ARRIVAL':
            update_move_id_row(current_move.row_id,
                               new_update='Confirmed Arrival at destination')
            current_move.status = 'DROPPING_OFF'
            new_move_log('GATE_IN', current_user.email, shuttle_id, scac, current_move_id)

        elif new_status == 'FORCE_TO_COMPLETED':
            current_move.ss_status = 'Completed'
            current_move.status = 'DELIVERED'
            update_move_id_row(current_move.row_id,
                               new_status='Completed',
                               new_update='Forced to completed.')
            update_move_status(scac, shuttle_id, 'FORCE_TO_COMPLETED')
            # TODO: MOVE TO COMPLETED MOVE LOG
            new_move_log('COMPLETED', current_user.email, shuttle_id, scac, current_move_id)

            if opposite_direction_move_id:
                next_move = OpenMoves.query.filter_by(driver_id=shuttle_id, move_id=opposite_direction_move_id).first()
                if next_move:
                    next_move.status = 'SEARCHING'
                    update_move_status(scac, shuttle_id, 'SEARCHING', container_number=next_move.container_number, destination=next_move.destination)
                new_move_log('ASSIGNED_FROM_NEXT', current_user.email, shuttle_id, scac, current_move_id)

        db.session.commit()
        return redirect(url_for('driver_w_shuttle_id', raw_shuttle_id=shuttle_id))

    return redirect(url_for('driver_w_shuttle_id', raw_shuttle_id=shuttle_id))


@app.route('/moves', methods=['GET'])
@login_required
def moves():
    update_workflow_list()
    move_list = []
    for i in workflow.values():
        move_list.append(i)
    return render_template('home/moves.html', moves=move_list)


@app.route('/moves/<move_ID>', methods=['GET', 'POST'])
@login_required
def single_move_id(move_ID):
    update_workflow_list()
    move = workflow.get(move_ID, None)
    if move:
        scac = move[8]
        driver_id = move[10]
        return render_template('home/move.html',
                               move_id=move[0],
                               container_number=move[2],
                               load_status=move[3],
                               priority=move[4],
                               customer=move[5],
                               origin=move[6],
                               destination=move[7],
                               scac=scac,
                               truck_number=move[9],
                               driver_id=driver_id,
                               )
    return str(move) + ' - doesnt exist'


@app.route('/api/get_move/<move_id>', methods=['GET'])
def get_move(move_id):
    move = workflow.get(move_id, False)
    reply = {}
    if move:
        reply = {
            'origin': move[6],
            'destination': move[7],
            'move_id': move[0],
            'row_id': move[1],
            'container_number': move[2],
            'load_status': move[3],
            'priority': move[4],
            'customer': move[5],
            'status': move[11]
        }

    # TODO: REDO TO REPLY TO BOTS REQUESTS


@app.route('/site_log', methods=['GET'])
@login_required
def site_log():
    log = SiteLog.query.all()
    printed_log = 'NONE<br>'
    for i in log:
        printed_log += i.action_type + ' ' + str(i.by_user) + ' ' + str(i.affected_user) + ' ' + str(i.created_at) + ' ' + str(
            i.modified_at) + ' ' + str(i.detailed_info) + '<br>'
    return printed_log


@app.route('/move_log', methods=['GET'])
@login_required
def move_log():
    log = MoveLog.query.all()
    printed_log = 'NONE<br>'
    for i in log:
        printed_log += i.action_type + ' ' + str(i.by_user) + ' ' + str(i.driver_id) + ' ' + str(i.scac) + ' ' + str(
            i.move_id) + ' ' + str(i.detailed_info) + ' ' + str(i.created_at) + '<br>'
    return printed_log


@app.route('/open_moves', methods=['GET', 'POST'])
@login_required
def open_moves():
    log = OpenMoves.query.all()
    printed_log = 'NONE<br>'
    for i in log:
        printed_log += str(i.move_id) + ' '
        printed_log += str(i.row_id) + ' '
        printed_log += str(i.driver_id) + ' '
        printed_log += str(i.container_number) + ' '
        printed_log += str(i.origin) + ' '
        printed_log += str(i.destination) + ' '
        printed_log += str(i.status) + ' '
        printed_log += str(i.created_at) + ' '
        printed_log += str(i.modified_at) + '<br>'
    return printed_log


def assign_current_move(scac, driver_id, move_id, origin=None, destination=None, container_number=None):
    data = {'driver_id': driver_id,
            'move_id': move_id}
    if move_id == 'BOBTAIL':
        data.update({
            'origin': origin,
            'destination': destination
        })
    else:
        data.update({
            'origin': origin,
            'destination': destination,
            'container_number': container_number
        })
    return requests.post(f'{carriers.get(scac)}move/current/assign', json=data)


def un_assign_current_move(scac, driver_id):
    data = {
        'driver_id': driver_id
    }
    reply = requests.post(f'{carriers.get(scac)}move/current/unassign', json=data)
    return reply


def assign_next_move(scac, driver_id, move_id):
    data = {
        'driver_id': driver_id,
        'move_id': move_id,
    }
    reply = requests.post(f'{carriers.get(scac)}move/next/assign', json=data)
    return reply


def un_assign_next_move(scac, driver_id):
    data = {
        'driver_id': driver_id
    }
    reply = requests.post(f'{carriers.get(scac)}move/next/unassign', json=data)
    return reply


def update_move_status(scac, driver_id, new_status, container_number=None, destination=None):
    data = {
        'driver_id': driver_id,
        'new_status': new_status
    }
    if new_status == 'SEARCHING':
        data.update({
            'container_number': container_number,
            'destination': destination
        })
    reply = requests.post(f'{carriers.get(scac)}move/current/new_status', json=data)
    return reply


@app.route('/test', methods=['GET', 'POST'])
@login_required
def test():
    some_thing = OpenMoves.query.all()
    for i in some_thing:
        db.session.delete(i)
        db.session.commit()
    return 'DELETED', 200

    return str(current_user.type)

    if request.method == 'GET':
        return render_template('home/test.html')

    new_user_type = request.form['change_user_type']
    if hasattr(new_user_type, '__iter__') and not isinstance(new_user_type, str):
        new_user_type = new_user_type[0]

    user = User.query.filter_by(id=int(current_user.get_real_id())).first()
    user.type = new_user_type
    db.session.commit()
    return redirect(url_for('test'), 302)

    import time
    st = time.time()
    # r = requests.get(f'{carriers.get("BMKJ")}get_driver/BM-1391', )
    update_workflow_list()
    et = time.time()
    elapsed_time = et - st
    return f'Execution time: {elapsed_time} seconds'


def update_move_id_row(row_id, new_scac=None, new_truck=None, new_driver_id=None, new_status=None, new_comment=None,
                       new_update=None):
    row_to_update = ss_client.Sheets.get_row(
        OPEN_MOVES_LOG_SHEETS,
        row_id
    )

    try:
        if row_to_update.result:
            return False
    except:
        pass

    new_row = smartsheet.models.Row()
    new_row.id = row_id

    if new_scac is not None:
        new_cell = smartsheet.models.Cell()
        new_cell.column_id = 4303620233553796  # 'Shuttle Provider SCAC'
        new_cell.value = new_scac if new_scac else ''
        new_row.cells.append(new_cell)

    if new_driver_id is not None:
        new_cell = smartsheet.models.Cell()
        new_cell.column_id = 222233071249284  # 'Driver Name (Last, First)'
        new_cell.value = new_driver_id if new_driver_id else ''
        new_row.cells.append(new_cell)

    if new_truck is not None:
        new_cell = smartsheet.models.Cell()
        new_cell.column_id = 8807219860924292  # 'Truck Number'
        new_cell.value = new_truck if new_truck else ''
        new_row.cells.append(new_cell)

    if new_status is not None:
        new_cell = smartsheet.models.Cell()
        new_cell.column_id = 4725832698619780  # 'Status'
        new_cell.value = new_status if new_status else ''
        new_row.cells.append(new_cell)

    if new_comment is not None:
        new_cell = smartsheet.models.Cell()
        new_cell.column_id = 2474032884934532  # 'Comments'
        new_cell.value = new_comment if new_comment else ''
        new_row.cells.append(new_cell)

    updated_row = ss_client.Sheets.update_rows(
        OPEN_MOVES_LOG_SHEETS,  # sheet_id
        [new_row]  # an array of rows to update
    )

    if new_update is not None:
        pass  # TODO: add a comment on a row, this is a placeholder.

    return updated_row


@app.route('/get_move', methods=['GET'])
def get_move_info_for_telebot():
    driver_id = request.headers.get('driver_id')
    move_id = request.headers.get('move_id')

    move = OpenMoves.query.filter_by(driver_id=driver_id, move_id=move_id).first()

    if move is None:
        return 'OK', 404

    reply = {
        'driver_id': driver_id,
        'move_id': move_id,
        'container_number': None if move.container_number is None else move.container_number,
        'destination': move.destination
    }

    return reply, 200
