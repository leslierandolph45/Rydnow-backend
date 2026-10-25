'use client';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';


const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'https://rydnow-backend-77.onrender.com/api';


// 🇬🇭 FLEXIBLE GHANA NUMBER VALIDATION
const isValidGhanaNumber = (phone: string) => {
  const cleaned = phone.replace(/\s/g, '');
 
  // ✅ Format 1: 0241234567 (10 digits starting with 0)
  if (/^0[2-9]\d{8}$/.test(cleaned)) return true;
 
  // ✅ Format 2: +233241234567 (starts with +233, then 9 digits)
  if (/^\+233[2-9]\d{8}$/.test(cleaned)) return true;
 
  // ✅ Format 3: 241234567 (9 digits only)
  if (/^[2-9]\d{8}$/.test(cleaned)) return true;
 
  return false;
};


export default function CustomerSignupPage() {
  const router = useRouter();
  const [step, setStep] = useState(1);
  const [formData, setFormData] = useState({
    fullName: '',
    email: '',
    phone: '',
    password: ''
  });
  const [verificationCode, setVerificationCode] = useState('');
  const [tempEmail, setTempEmail] = useState('');
  const [tempUsername, setTempUsername] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [successMsg, setSuccessMsg] = useState('');


  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setFormData(prev => ({ ...prev, [e.target.name]: e.target.value }));
    setError('');
  };


  const handleSubmitForm = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSuccessMsg('');


    if (!isValidGhanaNumber(formData.phone)) {
      setError('❌ Enter a valid Ghanaian number — e.g. 0241234567 or +233241234567');
      return;
    }


    setLoading(true);


    // Normalize phone — always send clean format
    const cleanPhone = formData.phone.replace(/\s/g, '');
    let finalPhone = cleanPhone;
    if (cleanPhone.startsWith('+233')) {
      finalPhone = '0' + cleanPhone.slice(4); // Convert +23324... → 024...
    } else if (cleanPhone.length === 9 && /^\d+$/.test(cleanPhone)) {
      finalPhone = '0' + cleanPhone; // Convert 9-digit → 0xxxxxxxxx
    }


    const username = formData.email.split('@')[0].replace(/[^a-zA-Z0-9]/g, '_');
    setTempUsername(username);
    setTempEmail(formData.email);


    try {
      // ✅ CORRECTED ENDPOINT
      const registerRes = await fetch(`${API_BASE}/auth/register/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: username,
          email: formData.email,
          password: formData.password,
          full_name: formData.fullName,
          phone: finalPhone
        })
      });


      const registerData = await registerRes.json();


      if (!registerRes.ok) {
        throw new Error(registerData.detail || registerData.username || registerData.email || '❌ Could not create account. Please try again.');
      }


      // ✅ CORRECTED ENDPOINT
      const codeRes = await fetch(`${API_BASE}/auth/send-code/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: formData.email })
      });


      const codeData = await codeRes.json();


      if (codeData.verification_code) {
        setSuccessMsg(`✅ Account created!\n📧 Verification Code: ${codeData.verification_code}`);
      } else {
        setSuccessMsg('✅ Account created! Check your email for the verification code.');
      }
     
      setStep(2);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };


  const handleVerifyCode = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSuccessMsg('');


    if (!verificationCode || verificationCode.length !== 6) {
      setError('❌ Please enter a valid 6-digit code.');
      return;
    }


    setLoading(true);
    try {
      // ✅ CORRECTED ENDPOINT
      const verifyRes = await fetch(`${API_BASE}/auth/verify-code/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: tempEmail,
          code: verificationCode
        })
      });


      const verifyData = await verifyRes.json();


      if (!verifyRes.ok) {
        throw new Error(verifyData.detail || '❌ Invalid or expired code.');
      }


      // ✅ CORRECTED ENDPOINT
      const loginRes = await fetch(`${API_BASE}/auth/login/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: tempUsername,
          password: formData.password
        })
      });


      const loginData = await loginRes.json();


      if (!loginRes.ok) {
        throw new Error(loginData.detail || '✅ Verified! Please log in manually.');
      }


      localStorage.setItem('rydnow_token', loginData.token);
      localStorage.setItem('rydnow_username', loginData.user.username);
      localStorage.setItem('authToken', loginData.token);
      localStorage.setItem('currentUser', JSON.stringify({
        id: loginData.user.id,
        username: loginData.user.username,
        email: loginData.user.email,
        full_name: formData.fullName,
        phone: formData.phone,
        type: 'customer',
        is_verified: true
      }));


      setStep(3);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };


  const resendCode = async () => {
    setError('');
    setSuccessMsg('');
    setLoading(true);
    try {
      // ✅ CORRECTED ENDPOINT
      const res = await fetch(`${API_BASE}/auth/send-code/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: tempEmail })
      });
      const data = await res.json();
      if (data.verification_code) {
        setSuccessMsg(`📧 New code sent! Code: ${data.verification_code}`);
      } else {
        setSuccessMsg('📧 New code sent! Please check your email.');
      }
    } catch (err: any) {
      setError(err.message || '❌ Failed to resend code.');
    } finally {
      setLoading(false);
    }
  };


  return (
    <div style={{
      minHeight: '100vh',
      backgroundColor: '#0F172A',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '24px',
      fontFamily: 'system-ui, -apple-system, sans-serif'
    }}>
      <div style={{ maxWidth: '420px', width: '100%' }}>
        <Link href="/login" style={{ color: '#94A3B8', textDecoration: 'none', fontSize: '14px', display: 'inline-block', marginBottom: '24px' }}>
          ← Back to Login
        </Link>


        {step === 1 && (
          <>
            <h1 style={{ fontSize: '28px', color: '#FFFFFF', marginBottom: '8px', fontWeight: 700 }}>Create Account</h1>
            <p style={{ color: '#94A3B8', marginBottom: '32px', fontSize: '15px' }}>Join Rydnow as a customer 🇬🇭</p>


            {error && (
              <div style={{ backgroundColor: '#7F1D1D', border: '1px solid #EF4444', padding: '12px 16px', borderRadius: '8px', marginBottom: '24px', color: '#FCA5A5', fontSize: '14px' }}>
                {error}
              </div>
            )}


            {successMsg && (
              <div style={{ backgroundColor: '#1E3A2F', border: '1px solid #10B981', padding: '12px 16px', borderRadius: '8px', marginBottom: '24px', color: '#86EFAC', fontSize: '14px', whiteSpace: 'pre-line' }}>
                {successMsg}
              </div>
            )}


            <form onSubmit={handleSubmitForm}>
              <div style={{ marginBottom: '16px' }}>
                <label style={{ display: 'block', marginBottom: '8px', fontSize: '14px', color: '#CBD5E1', fontWeight: 500 }}>Full Name</label>
                <input
                  type="text"
                  name="fullName"
                  value={formData.fullName}
                  onChange={handleChange}
                  placeholder="John Doe"
                  required
                  style={{
                    width: '100%',
                    padding: '14px 16px',
                    backgroundColor: '#1E293B',
                    border: '1px solid #475569',
                    borderRadius: '8px',
                    color: '#FFFFFF',
                    fontSize: '15px',
                    boxSizing: 'border-box',
                    outline: 'none'
                  }}
                />
              </div>


              <div style={{ marginBottom: '16px' }}>
                <label style={{ display: 'block', marginBottom: '8px', fontSize: '14px', color: '#CBD5E1', fontWeight: 500 }}>Email Address</label>
                <input
                  type="email"
                  name="email"
                  value={formData.email}
                  onChange={handleChange}
                  placeholder="your@email.com"
                  required
                  style={{
                    width: '100%',
                    padding: '14px 16px',
                    backgroundColor: '#1E293B',
                    border: '1px solid #475569',
                    borderRadius: '8px',
                    color: '#FFFFFF',
                    fontSize: '15px',
                    boxSizing: 'border-box',
                    outline: 'none'
                  }}
                />
              </div>


              <div style={{ marginBottom: '16px' }}>
                <label style={{ display: 'block', marginBottom: '8px', fontSize: '14px', color: '#CBD5E1', fontWeight: 500 }}>Phone Number 🇬🇭</label>
                <input
                  type="tel"
                  name="phone"
                  value={formData.phone}
                  onChange={handleChange}
                  placeholder="0241234567 or +233241234567"
                  required
                  style={{
                    width: '100%',
                    padding: '14px 16px',
                    backgroundColor: '#1E293B',
                    border: '1px solid #475569',
                    borderRadius: '8px',
                    color: '#FFFFFF',
                    fontSize: '15px',
                    boxSizing: 'border-box',
                    outline: 'none'
                  }}
                />
              </div>


              <div style={{ marginBottom: '24px' }}>
                <label style={{ display: 'block', marginBottom: '8px', fontSize: '14px', color: '#CBD5E1', fontWeight: 500 }}>Create Password</label>
                <input
                  type="password"
                  name="password"
                  value={formData.password}
                  onChange={handleChange}
                  placeholder="At least 6 characters"
                  required
                  style={{
                    width: '100%',
                    padding: '14px 16px',
                    backgroundColor: '#1E293B',
                    border: '1px solid #475569',
                    borderRadius: '8px',
                    color: '#FFFFFF',
                    fontSize: '15px',
                    boxSizing: 'border-box',
                    outline: 'none'
                  }}
                />
              </div>


              <button
                type="submit"
                disabled={loading}
                style={{
                  width: '100%',
                  padding: '15px',
                  backgroundColor: '#3B82F6',
                  color: '#FFFFFF',
                  border: 'none',
                  borderRadius: '8px',
                  fontSize: '16px',
                  fontWeight: 600,
                  cursor: loading ? 'not-allowed' : 'pointer',
                  opacity: loading ? 0.7 : 1
                }}
              >
                {loading ? 'Creating Account...' : 'Create Account'}
              </button>
            </form>
          </>
        )}


        {step === 2 && (
          <>
            <h1 style={{ fontSize: '28px', color: '#FFFFFF', marginBottom: '8px', fontWeight: 700 }}>Verify Your Account</h1>
            <p style={{ color: '#94A3B8', marginBottom: '16px', fontSize: '15px' }}>We sent a 6-digit code to your<br />email: <strong>{tempEmail}</strong></p>


            {error && (
              <div style={{ backgroundColor: '#7F1D1D', border: '1px solid #EF4444', padding: '12px', borderRadius: '8px', marginBottom: '16px', color: '#FCA5A5', fontSize: '14px' }}>
                {error}
              </div>
            )}


            {successMsg && (
              <div style={{ backgroundColor: '#1E3A2F', border: '1px solid #10B981', padding: '12px', borderRadius: '8px', marginBottom: '16px', color: '#86EFAC', fontSize: '14px' }}>
                {successMsg}
              </div>
            )}


            <form onSubmit={handleVerifyCode}>
              <div style={{ marginBottom: '24px' }}>
                <label style={{ display: 'block', marginBottom: '8px', fontSize: '14px', color: '#CBD5E1', fontWeight: 500 }}>Enter Verification Code</label>
                <input
                  type="text"
                  value={verificationCode}
                  onChange={(e) => setVerificationCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                  placeholder="000000"
                  maxLength={6}
                  required
                  style={{
                    width: '100%',
                    padding: '16px',
                    backgroundColor: '#1E293B',
                    border: '1px solid #475569',
                    borderRadius: '8px',
                    color: '#FFFFFF',
                    fontSize: '24px',
                    textAlign: 'center',
                    letterSpacing: '8px',
                    boxSizing: 'border-box',
                    outline: 'none'
                  }}
                />
              </div>


              <button
                type="submit"
                disabled={loading}
                style={{
                  width: '100%',
                  padding: '15px',
                  backgroundColor: '#3B82F6',
                  color: '#FFFFFF',
                  border: 'none',
                  borderRadius: '8px',
                  fontSize: '16px',
                  fontWeight: 600,
                  cursor: loading ? 'not-allowed' : 'pointer',
                  marginBottom: '12px',
                  opacity: loading ? 0.7 : 1
                }}
              >
                {loading ? 'Verifying...' : 'Verify & Continue'}
              </button>


              <button
                type="button"
                onClick={resendCode}
                disabled={loading}
                style={{
                  width: '100%',
                  padding: '12px',
                  backgroundColor: 'transparent',
                  color: '#94A3B8',
                  border: 'none',
                  fontSize: '14px',
                  cursor: loading ? 'not-allowed' : 'pointer'
                }}
              >
                {loading ? 'Sending...' : 'Resend Code'}
              </button>
            </form>
          </>
        )}


        {step === 3 && (
          <div style={{ textAlign: 'center', padding: '20px 0' }}>
            <div style={{ fontSize: '60px', marginBottom: '20px' }}>✅</div>
            <h2 style={{ fontSize: '24px', color: '#FFFFFF', marginBottom: '8px', fontWeight: 600 }}>Welcome, {formData.fullName}!</h2>
            <p style={{ color: '#94A3B8', marginBottom: '24px' }}>Your account is verified and ready.</p>
            <Link href="/customer-dashboard" style={{ display: 'inline-block', width: '100%', padding: '15px', backgroundColor: '#3B82F6', color: '#FFFFFF', border: 'none', borderRadius: '8px', fontSize: '16px', fontWeight: 600, textAlign: 'center', textDecoration: 'none' }}>
              Go to My Dashboard →
            </Link>
          </div>
        )}


        {step !== 3 && (
          <div style={{ marginTop: '24px', textAlign: 'center', paddingTop: '20px', borderTop: '1px solid #1E293B' }}>
            <p style={{ fontSize: '14px', color: '#64748B', margin: 0 }}>
              Already have an account?{' '}
              <Link href="/login" style={{ color: '#3B82F6', textDecoration: 'none', fontWeight: 600 }}>
                Sign In →
              </Link>
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
