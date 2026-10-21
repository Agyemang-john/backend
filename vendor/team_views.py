"""
vendor/team_views.py
Store team management: the owner (and admins, for staff) invite people by
email; each person joins with their OWN Negromart account and signs in to the
seller dashboard with it. Nobody shares the owner's password.

Dashboard (vendor cookie, X-User-Type: vendor):
    GET    /api/v1/vendor/team/                              members + pending invites
    POST   /api/v1/vendor/team/invitations/                  {email, role}
    POST   /api/v1/vendor/team/invitations/<id>/resend/
    DELETE /api/v1/vendor/team/invitations/<id>/             revoke
    PATCH  /api/v1/vendor/team/members/<id>/                 {role}  (owner only)
    GET    /api/v1/vendor/team/members/<id>/activity/        sign-in history
    DELETE /api/v1/vendor/team/members/<id>/                 remove, or leave (self)

Invitee (public link, then customer cookie, X-User-Type: customer):
    GET    /api/v1/vendor/team/invitations/lookup/?token=…   what am I invited to?
    POST   /api/v1/vendor/team/invitations/accept/           {token}

Who may do what:
    owner → invite/remove admins and staff, change roles
    admin → invite/remove staff only (cannot grant or remove admin rights)
    staff → no team management; anyone except the owner may leave

Sign-in times and last-active are visible to the people who manage that member
(owner → everyone, admin → staff) and to each person for themselves.
"""

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, OuterRef, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from .access import (
    Capability, IsVendorMember, capabilities_for_role, clear_membership_cache,
    get_membership, require_capability,
)
from .models import VendorInvitation, VendorMember
from .team_serializers import (
    AcceptInvitationSerializer, CreateInvitationSerializer, UpdateMemberSerializer,
    VendorInvitationSerializer, VendorMemberSerializer,
)

logger = logging.getLogger(__name__)

def _max_team_members(vendor):
    # Members + pending invites, set per subscription plan (owner included).
    from payments.entitlements import team_member_limit
    return team_member_limit(vendor)


class TeamInviteThrottle(UserRateThrottle):
    scope = 'vendor_team_invite'
    rate = '30/hour'


class InviteLookupThrottle(AnonRateThrottle):
    scope = 'vendor_team_invite_lookup'
    rate = '60/hour'


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pending_invites(vendor):
    return VendorInvitation.objects.filter(
        vendor=vendor, accepted_at__isnull=True, revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    )


def _can_manage_role(actor_role, target_role):
    """Owners manage admins and staff; admins manage staff only."""
    if actor_role == VendorMember.ROLE_OWNER:
        return target_role in (VendorMember.ROLE_ADMIN, VendorMember.ROLE_STAFF)
    if actor_role == VendorMember.ROLE_ADMIN:
        return target_role == VendorMember.ROLE_STAFF
    return False


def _issue_invitation(invitation):
    """
    Give the invite a fresh token + expiry and email the link. Only the hash
    is stored; the raw token exists just long enough to go into the email.
    """
    raw_token = secrets.token_urlsafe(32)
    invitation.token_hash = VendorInvitation.hash_token(raw_token)
    invitation.expires_at = timezone.now() + timedelta(days=VendorInvitation.TTL_DAYS)
    invitation.save()

    from .tasks import send_team_invitation_email
    # on_commit: never email a link whose row could still be rolled back.
    transaction.on_commit(lambda: send_team_invitation_email.delay(invitation.pk, raw_token))


def _forbidden(detail):
    return Response({'detail': detail}, status=status.HTTP_403_FORBIDDEN)


def _invalidate_member_access(user):
    """Sign a removed member out of the seller dashboard everywhere."""
    from userauths.models import UserSession
    UserSession.objects.filter(user=user, is_vendor_session=True).delete()
    try:
        from django_redis import get_redis_connection
        get_redis_connection("default").delete(f"vendor:uid_vid:{user.pk}")
    except Exception:
        pass
    clear_membership_cache(user)


# Shown as "Active now" when the person made a dashboard request this recently.
# The dashboard heartbeat pings every 10 minutes while a tab is visible.
ONLINE_WINDOW = timedelta(minutes=15)


def _can_view_activity(actor, target):
    return (
        target.pk == actor.pk
        or actor.role == VendorMember.ROLE_OWNER
        or _can_manage_role(actor.role, target.role)
    )


def _with_activity(members):
    """Annotate sign-in / sign-out / session info onto a VendorMember queryset."""
    from userauths.models import SecurityEvent, UserSession

    def latest(event, field):
        # Only events since they joined this store.
        return Subquery(
            SecurityEvent.objects.filter(
                user=OuterRef('user'), is_vendor=True, event=event,
                created_at__gte=OuterRef('created_at'),
            ).order_by('-created_at').values(field)[:1]
        )

    sessions = UserSession.objects.filter(user=OuterRef('user'), is_vendor_session=True).values('user')
    return members.annotate(
        last_sign_in_at=latest(SecurityEvent.SIGN_IN, 'created_at'),
        last_sign_in_device=latest(SecurityEvent.SIGN_IN, 'device_name'),
        last_sign_out_at=latest(SecurityEvent.SIGN_OUT, 'created_at'),
        session_last_activity=Subquery(sessions.annotate(m=Max('last_activity')).values('m')[:1]),
        active_sessions=Subquery(sessions.annotate(c=Count('pk')).values('c')[:1]),
    )


def _member_last_seen(user_ids):
    """user_id → aware datetime of their last dashboard request (Redis), where known."""
    if not user_ids:
        return {}
    try:
        from datetime import datetime, timezone as dt_timezone
        from django_redis import get_redis_connection
        from .middleware import MEMBER_SEEN_PREFIX
        values = get_redis_connection("default").mget([f"{MEMBER_SEEN_PREFIX}{uid}" for uid in user_ids])
        return {
            uid: datetime.fromtimestamp(int(raw), tz=dt_timezone.utc)
            for uid, raw in zip(user_ids, values) if raw
        }
    except Exception:
        return {}


def _activity_summary(member, seen):
    candidates = [t for t in (seen, member.session_last_activity, member.last_sign_in_at) if t]
    last_active = max(candidates) if candidates else None
    return {
        'last_sign_in_at': member.last_sign_in_at,
        'last_sign_in_device': member.last_sign_in_device or '',
        'last_sign_out_at': member.last_sign_out_at,
        'last_active_at': last_active,
        'is_online': bool(
            last_active and member.active_sessions
            and timezone.now() - last_active <= ONLINE_WINDOW
        ),
        'active_sessions': member.active_sessions or 0,
    }


# ── Dashboard: team management ────────────────────────────────────────────────

class TeamListView(APIView):
    """GET /api/v1/vendor/team/: every member may see who is on the team."""
    permission_classes = [IsAuthenticated, IsVendorMember]

    def get(self, request):
        membership = get_membership(request.user)
        vendor = membership.vendor
        members = list(_with_activity(
            VendorMember.objects.filter(vendor=vendor, is_active=True)
            .select_related('user').order_by('created_at')
        ))
        visible = [m for m in members if _can_view_activity(membership, m)]
        seen = _member_last_seen([m.user_id for m in visible])
        activity = {m.pk: _activity_summary(m, seen.get(m.user_id)) for m in visible}
        can_manage = Capability.MANAGE_TEAM in capabilities_for_role(membership.role)
        invitations = (
            _pending_invites(vendor).select_related('invited_by') if can_manage
            else VendorInvitation.objects.none()
        )
        return Response({
            'me': {
                'member_id': membership.pk,
                'role': membership.role,
                'capabilities': sorted(capabilities_for_role(membership.role)),
                # Which roles this user may hand out (drives the invite form).
                'can_invite_roles': [
                    r for r in (VendorMember.ROLE_ADMIN, VendorMember.ROLE_STAFF)
                    if _can_manage_role(membership.role, r)
                ],
            },
            'members': VendorMemberSerializer(
                members, many=True, context={'request': request, 'activity': activity},
            ).data,
            'invitations': VendorInvitationSerializer(invitations, many=True).data,
            'limits': {
                'max_members': _max_team_members(vendor),
                'used': len(members) + _pending_invites(vendor).count(),
            },
        })


class TeamInvitationCreateView(APIView):
    """POST /api/v1/vendor/team/invitations/  {email, role}"""
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_TEAM)]
    throttle_classes = [TeamInviteThrottle]

    def post(self, request):
        serializer = CreateInvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email, role = serializer.validated_data['email'], serializer.validated_data['role']

        membership = get_membership(request.user)
        vendor = membership.vendor
        if not _can_manage_role(membership.role, role):
            return _forbidden("Only the store owner can invite admins.")

        if VendorMember.objects.filter(vendor=vendor, is_active=True, user__email__iexact=email).exists():
            return Response({'email': ['This person is already on your team.']},
                            status=status.HTTP_400_BAD_REQUEST)
        if _pending_invites(vendor).filter(email__iexact=email).exists():
            return Response({'email': ['An invitation is already pending for this email. Resend it instead.']},
                            status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            # Lock the store row so two concurrent invites can't both slip
            # under the team-size limit.
            type(vendor).objects.select_for_update().filter(pk=vendor.pk).first()
            used = (
                VendorMember.objects.filter(vendor=vendor, is_active=True).count()
                + _pending_invites(vendor).count()
            )
            limit = _max_team_members(vendor)
            if used >= limit:
                return Response(
                    {'detail': (
                        'Your plan includes only the store owner. Upgrade to invite team members.'
                        if limit <= 1 else
                        f'Your plan allows {limit} people including pending invites. '
                        'Remove someone, revoke an invite, or upgrade your plan.'
                    ), 'code': 'team_full', 'limit': limit},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            invitation = VendorInvitation(
                vendor=vendor, email=email, role=role, invited_by=request.user,
            )
            _issue_invitation(invitation)

        logger.info("team: %s invited %s as %s to vendor=%s", request.user.pk, email, role, vendor.pk)
        return Response(VendorInvitationSerializer(invitation).data, status=status.HTTP_201_CREATED)


class TeamInvitationDetailView(APIView):
    """
    POST   /api/v1/vendor/team/invitations/<id>/resend/: new link, new 7-day expiry
    DELETE /api/v1/vendor/team/invitations/<id>/: revoke
    """
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_TEAM)]
    throttle_classes = [TeamInviteThrottle]

    def _get(self, request, invitation_id):
        membership = get_membership(request.user)
        invitation = get_object_or_404(
            VendorInvitation, pk=invitation_id, vendor=membership.vendor,
            accepted_at__isnull=True, revoked_at__isnull=True,
        )
        if not _can_manage_role(membership.role, invitation.role):
            return membership, None
        return membership, invitation

    def post(self, request, invitation_id):
        _, invitation = self._get(request, invitation_id)
        if invitation is None:
            return _forbidden("Only the store owner can manage admin invitations.")
        with transaction.atomic():
            _issue_invitation(invitation)
        return Response(VendorInvitationSerializer(invitation).data)

    def delete(self, request, invitation_id):
        _, invitation = self._get(request, invitation_id)
        if invitation is None:
            return _forbidden("Only the store owner can manage admin invitations.")
        invitation.revoked_at = timezone.now()
        invitation.save(update_fields=['revoked_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)


class TeamMemberDetailView(APIView):
    """
    PATCH  /api/v1/vendor/team/members/<id>/  {role}: owner changes admin ⇄ staff
    DELETE /api/v1/vendor/team/members/<id>/: remove a member, or leave (own id)
    """
    permission_classes = [IsAuthenticated, IsVendorMember]

    def _target(self, request, member_id):
        membership = get_membership(request.user)
        target = get_object_or_404(
            VendorMember.objects.select_related('user'),
            pk=member_id, vendor=membership.vendor, is_active=True,
        )
        return membership, target

    def patch(self, request, member_id):
        membership, target = self._target(request, member_id)
        if membership.role != VendorMember.ROLE_OWNER:
            return _forbidden("Only the store owner can change team roles.")
        if target.role == VendorMember.ROLE_OWNER:
            return _forbidden("The owner's role cannot be changed here. Contact support to transfer ownership.")

        serializer = UpdateMemberSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target.role = serializer.validated_data['role']
        target.save(update_fields=['role'])
        logger.info("team: owner %s set member=%s role=%s", request.user.pk, target.pk, target.role)
        return Response(VendorMemberSerializer(target, context={'request': request}).data)

    def delete(self, request, member_id):
        membership, target = self._target(request, member_id)
        leaving = target.pk == membership.pk

        if target.role == VendorMember.ROLE_OWNER:
            return _forbidden("The store owner cannot be removed. Contact support to transfer or close the store.")
        if not leaving:
            if Capability.MANAGE_TEAM not in capabilities_for_role(membership.role):
                return _forbidden("Your team role does not allow removing members.")
            if not _can_manage_role(membership.role, target.role):
                return _forbidden("Only the store owner can remove admins.")

        target.is_active = False
        target.removed_at = timezone.now()
        target.save(update_fields=['is_active', 'removed_at'])
        _invalidate_member_access(target.user)
        logger.info("team: member=%s %s by user=%s", target.pk, 'left' if leaving else 'removed', request.user.pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TeamMemberActivityView(APIView):
    """
    GET /api/v1/vendor/team/members/<id>/activity/?limit=50

    The member's seller-dashboard security timeline (sign-ins, sign-outs,
    devices signed out, password changes) since they joined this store.
    """
    permission_classes = [IsAuthenticated, IsVendorMember]

    def get(self, request, member_id):
        from userauths.models import SecurityEvent

        membership = get_membership(request.user)
        target = get_object_or_404(
            _with_activity(VendorMember.objects.select_related('user')),
            pk=member_id, vendor=membership.vendor, is_active=True,
        )
        if not _can_view_activity(membership, target):
            return _forbidden("You can only see sign-in activity for people you manage.")

        try:
            limit = max(1, min(int(request.query_params.get('limit', 50)), 100))
        except ValueError:
            limit = 50
        events = SecurityEvent.objects.filter(
            user_id=target.user_id, is_vendor=True, created_at__gte=target.created_at,
        ).order_by('-created_at')[:limit]

        labels = dict(SecurityEvent.EVENT_CHOICES)
        seen = _member_last_seen([target.user_id]).get(target.user_id)
        return Response({
            'member': VendorMemberSerializer(target, context={'request': request}).data,
            'summary': _activity_summary(target, seen),
            'events': [
                {
                    'id': e.pk,
                    'event': e.event,
                    'label': labels.get(e.event, e.event),
                    'created_at': e.created_at,
                    'device_name': e.device_name,
                    'device_type': e.device_type,
                    'ip_address': e.ip_address,
                    'new_device': e.new_device,
                }
                for e in events
            ],
        })


# ── Invitee side ──────────────────────────────────────────────────────────────

def _find_invitation(raw_token):
    if not raw_token:
        return None
    return (
        VendorInvitation.objects.select_related('vendor', 'invited_by')
        .filter(token_hash=VendorInvitation.hash_token(raw_token))
        .first()
    )


def _invitation_state(invitation):
    if invitation.accepted_at:
        return 'accepted'
    if invitation.revoked_at:
        return 'revoked'
    if invitation.expires_at <= timezone.now():
        return 'expired'
    return 'pending'


class TeamInvitationLookupView(APIView):
    """
    GET /api/v1/vendor/team/invitations/lookup/?token=…

    Public: the token itself is the credential. Tells the accept page what the
    invite is for and whether to show "sign in" or "create account" first.
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [InviteLookupThrottle]

    def get(self, request):
        from django.contrib.auth import get_user_model
        from userauths.verification import mask_email

        invitation = _find_invitation(request.query_params.get('token'))
        if invitation is None:
            return Response({'detail': 'This invitation link is invalid.', 'code': 'invalid'},
                            status=status.HTTP_404_NOT_FOUND)

        inviter = invitation.invited_by
        return Response({
            'status': _invitation_state(invitation),
            'store_name': invitation.vendor.name,
            'role': invitation.role,
            'capabilities': sorted(capabilities_for_role(invitation.role)),
            'email': invitation.email,
            'masked_email': mask_email(invitation.email),
            'invited_by_name': f"{inviter.first_name} {inviter.last_name}".strip() if inviter else None,
            'expires_at': invitation.expires_at,
            'account_exists': get_user_model().objects.filter(email__iexact=invitation.email).exists(),
        })


class TeamInvitationAcceptView(APIView):
    """
    POST /api/v1/vendor/team/invitations/accept/  {token}

    Called with the invitee's CUSTOMER session (they may not have dashboard
    access yet). The signed-in account must own the invited, verified email.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = AcceptInvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user

        invitation = _find_invitation(serializer.validated_data['token'])
        if invitation is None or _invitation_state(invitation) != 'pending':
            state = _invitation_state(invitation) if invitation else 'invalid'
            return Response(
                {'detail': {
                    'accepted': 'This invitation has already been used.',
                    'revoked': 'This invitation was cancelled by the store.',
                    'expired': 'This invitation has expired. Ask the store to send a new one.',
                }.get(state, 'This invitation link is invalid.'), 'code': state},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if user.email.lower() != invitation.email.lower():
            return Response(
                {'detail': 'This invitation was sent to a different email address. '
                           'Sign in with that account to accept it.', 'code': 'wrong_account'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not user.is_email_verified:
            return Response({'detail': 'Please verify your email address first.', 'code': 'email_unverified'},
                            status=status.HTTP_403_FORBIDDEN)

        current = get_membership(user)
        if current is not None:
            same_store = current.vendor_id == invitation.vendor_id
            return Response(
                {'detail': 'You are already on this store\'s team.' if same_store else
                           'Your account already belongs to another store. Leave that team first.',
                 'code': 'already_member' if same_store else 'team_member'},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            with transaction.atomic():
                # Reactivate a previous membership (removed earlier) or create one.
                member, created = VendorMember.objects.get_or_create(
                    vendor=invitation.vendor, user=user,
                    defaults={'role': invitation.role, 'added_by': invitation.invited_by},
                )
                if not created:
                    member.role = invitation.role
                    member.is_active = True
                    member.removed_at = None
                    member.added_by = invitation.invited_by
                    member.save(update_fields=['role', 'is_active', 'removed_at', 'added_by'])

                invitation.accepted_at = timezone.now()
                invitation.accepted_by = user
                invitation.save(update_fields=['accepted_at', 'accepted_by'])
        except IntegrityError:
            # Joined another store concurrently (uniq_active_membership_per_user).
            return Response({'detail': 'Your account already belongs to another store.', 'code': 'team_member'},
                            status=status.HTTP_409_CONFLICT)

        clear_membership_cache(user)
        logger.info("team: user=%s accepted invite=%s for vendor=%s", user.pk, invitation.pk, invitation.vendor_id)
        return Response({
            'detail': f'You have joined {invitation.vendor.name}.',
            'store_name': invitation.vendor.name,
            'role': member.role,
            # Suspended/unapproved stores keep the membership; access starts
            # when the store is approved again.
            'can_access_dashboard': invitation.vendor.is_approved,
        })
