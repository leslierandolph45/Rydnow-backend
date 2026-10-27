import os
import json
import requests
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction, IntegrityError
from django.conf import settings
from django.utils import timezone
from django.core.exceptions import ObjectDoesNotExist
from rest_framework.decorators import api_view, permission_classes, csrf_exempt
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from .models import Booking, WalletTransaction, RiderWallet, COMMISSION_RATE




@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def record_job_payment(request):
    """
    ✅ Secure payment verification & recording — Paystack-first flow.
    Idempotent: safe to call multiple times. No duplicate credits.
    Client supplies ONLY the reference — all other data verified server-side.
    """
    reference = request.data.get('reference')


    # ─── 1. Input validation ───
    if not reference or not isinstance(reference, str) or len(reference.strip()) < 8:
        return Response(
            {'detail': 'A valid payment reference is required.'},
            status=status.HTTP_400_BAD_REQUEST
        )
    reference = reference.strip()


    # ─── 2. Look up booking by payment_ref ───
    try:
        booking = Booking.objects.select_related('assigned_rider').get(
            payment_ref=reference
        )
    except Booking.DoesNotExist:
        return Response(
            {'detail': 'No booking found for this payment reference.'},
            status=status.HTTP_404_NOT_FOUND
        )


    # ─── 3. Idempotency: Check existing transaction FIRST ───
    existing_txn = None
    try:
        existing_txn = WalletTransaction.objects.get(reference=reference)
    except WalletTransaction.DoesNotExist:
        pass


    if existing_txn is not None:
        if existing_txn.related_booking_id != booking.id:
            return Response(
                {'detail': 'Payment reference is associated with a different record. Please contact support.'},
                status=status.HTTP_409_CONFLICT
            )
        return Response({
            'message': 'Payment already verified and recorded.',
            'booking_id': str(booking.id),
            'status': 'already_recorded',
            'total_amount': float(existing_txn.amount),
        }, status=status.HTTP_200_OK)


    # ─── 4. Check booking-level payment status ───
    if hasattr(booking, 'payment_status') and booking.payment_status in ['held', 'released']:
        return Response({
            'message': 'Payment already verified and recorded.',
            'booking_id': str(booking.id),
            'status': 'already_recorded'
        }, status=status.HTTP_200_OK)


    # ─── 5. Verify payment with Paystack ───
    PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY')
    if not PAYSTACK_SECRET_KEY:
        return Response(
            {'detail': 'Payment verification service is currently unavailable.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )


    verify_url = f'https://api.paystack.co/transaction/verify/{reference}'
    headers = {
        'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}',
        'Content-Type': 'application/json'
    }


    try:
        resp = requests.get(verify_url, headers=headers, timeout=30)
        if resp.status_code != 200:
            return Response(
                {'detail': 'Payment verification failed. Please retry shortly.'},
                status=status.HTTP_502_BAD_GATEWAY
            )
        data = resp.json()
    except requests.exceptions.RequestException:
        return Response(
            {'detail': 'Could not reach payment verification service. Please try again.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )
    except json.JSONDecodeError:
        return Response(
            {'detail': 'Invalid response from payment service.'},
            status=status.HTTP_502_BAD_GATEWAY
        )


    # ─── 6. Validate Paystack response ───
    if not isinstance(data, dict) or not data.get('status'):
        return Response(
            {'detail': 'Payment verification was not successful.'},
            status=status.HTTP_400_BAD_REQUEST
        )


    paystack_data = data.get('data', {})


    if paystack_data.get('status') != 'success':
        return Response(
            {'detail': 'Payment has not been completed successfully.'},
            status=status.HTTP_400_BAD_REQUEST
        )


    if paystack_data.get('reference') != reference:
        return Response(
            {'detail': 'Payment reference mismatch detected.'},
            status=status.HTTP_409_CONFLICT
        )


    if paystack_data.get('currency') != 'GHS':
        return Response(
            {'detail': 'Unsupported payment currency. Only GHS is accepted.'},
            status=status.HTTP_400_BAD_REQUEST
        )


    # ─── 7. Amount verification ───
    verified_amount_pesewas = paystack_data.get('amount')
    if not isinstance(verified_amount_pesewas, int) or verified_amount_pesewas <= 0:
        return Response(
            {'detail': 'Invalid payment amount from verification service.'},
            status=status.HTTP_400_BAD_REQUEST
        )


    verified_amount = Decimal(verified_amount_pesewas) / Decimal('100')
    booking_amount = Decimal(booking.total_fare)


    if abs(verified_amount - booking_amount) > Decimal('0.01'):
        return Response(
            {'detail': 'Payment amount does not match booking total.'},
            status=status.HTTP_409_CONFLICT
        )


    # ─── 8. Atomic transaction ───
    try:
        with transaction.atomic():
            booking_locked = Booking.objects.select_for_update().get(
                payment_ref=reference
            )


            if hasattr(booking_locked, 'payment_status') and booking_locked.payment_status in ['held', 'released']:
                return Response({
                    'message': 'Payment already verified and recorded.',
                    'booking_id': str(booking_locked.id),
                    'status': 'already_recorded'
                }, status=status.HTTP_200_OK)


            rider = getattr(booking_locked, 'assigned_rider', None)
            if rider is None:
                return Response(
                    {'detail': 'This booking has no assigned rider. Cannot record payment.'},
                    status=status.HTTP_400_BAD_REQUEST
                )


            commission_rate = getattr(settings, 'COMMISSION_RATE', COMMISSION_RATE)
            commission = (booking_amount * Decimal(str(commission_rate))).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            rider_amount = (booking_amount - commission).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )


            if hasattr(booking_locked, 'confirm_payment_verified'):
                booking_locked.confirm_payment_verified(
                    provider_ref=paystack_data.get('id', ''),
                    payment_ref=reference
                )


            wallet, _ = RiderWallet.objects.get_or_create(rider=rider)
            if hasattr(wallet, 'credit_earnings'):
                wallet_result, wallet_msg = wallet.credit_earnings(
                    rider_amount,
                    booking_ref=booking_locked.booking_ref,
                    payment_verified=True,
                    related_booking=booking_locked
                )
                if wallet_result is None:
                    return Response({'detail': wallet_msg}, status=status.HTTP_409_CONFLICT)


    except Booking.DoesNotExist:
        return Response(
            {'detail': 'Booking was removed during processing.'},
            status=status.HTTP_404_NOT_FOUND
        )
    except IntegrityError:
        return Response(
            {'detail': 'Payment recording conflict. Reference may already exist.'},
            status=status.HTTP_409_CONFLICT
        )


    # ─── 9. Success response ───
    return Response({
        'message': '✅ Payment verified successfully.',
        'booking_id': str(booking_locked.id),
        'booking_ref': booking_locked.booking_ref,
        'total_amount': float(booking_amount),
        'platform_commission': float(commission),
        'rider_amount': float(rider_amount),
        'payment_status': getattr(booking_locked, 'payment_status', 'confirmed')
    }, status=status.HTTP_201_CREATED)
