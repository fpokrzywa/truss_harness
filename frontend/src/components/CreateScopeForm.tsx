'use client';

import { useId, useState, type FormEvent } from 'react';
import { ApiError } from '@/lib/api/errors';
import { slugify, validateEmail, validateName, validateSlug } from '@/lib/validation';
import { ErrorMessage } from './ErrorMessage';

export interface CreateScopeValues {
  slug: string;
  name: string;
  description?: string;
  admin_email?: string;
}

interface CreateScopeFormProps {
  /** Lowercase singular noun used in labels, e.g. "workspace". */
  noun: string;
  withDescription?: boolean;
  withAdminEmail?: boolean;
  onSubmit: (values: CreateScopeValues) => Promise<unknown>;
}

type FieldErrors = Partial<Record<'slug' | 'name' | 'admin_email', string>>;

/**
 * Create form with client-side validation mirroring the API. The backend remains the
 * authority: a 403 disables the form with an explanation rather than hiding it.
 */
export function CreateScopeForm({
  noun,
  withDescription = true,
  withAdminEmail = false,
  onSubmit,
}: CreateScopeFormProps) {
  const uid = useId();
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [slugTouched, setSlugTouched] = useState(false);
  const [description, setDescription] = useState('');
  const [adminEmail, setAdminEmail] = useState('');
  const [errors, setErrors] = useState<FieldErrors>({});
  const [failure, setFailure] = useState<unknown>(null);
  const [forbidden, setForbidden] = useState(false);
  const [busy, setBusy] = useState(false);

  function onNameChange(value: string) {
    setName(value);
    if (!slugTouched) setSlug(slugify(value));
  }

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const next: FieldErrors = {};
    const nameErr = validateName(name);
    const slugErr = validateSlug(slug);
    const emailErr = withAdminEmail && adminEmail.trim() !== '' ? validateEmail(adminEmail) : null;
    if (nameErr) next.name = nameErr;
    if (slugErr) next.slug = slugErr;
    if (emailErr) next.admin_email = emailErr;
    setErrors(next);
    setFailure(null);
    if (Object.keys(next).length > 0) return;

    const values: CreateScopeValues = { slug, name: name.trim() };
    if (withDescription && description.trim() !== '') values.description = description.trim();
    if (withAdminEmail && adminEmail.trim() !== '') values.admin_email = adminEmail.trim();

    setBusy(true);
    try {
      await onSubmit(values);
      setName('');
      setSlug('');
      setSlugTouched(false);
      setDescription('');
      setAdminEmail('');
    } catch (err) {
      if (err instanceof ApiError && err.status === 403 && err.code !== 'csrf_failed') {
        setForbidden(true);
      } else if (err instanceof ApiError && err.status === 409) {
        setErrors({ slug: `That slug is already in use. Choose a different ${noun} slug.` });
        setFailure(err);
      } else if (err instanceof ApiError && err.status === 422) {
        const mapped: FieldErrors = {};
        for (const fe of err.fieldErrors) {
          const field = fe.loc[fe.loc.length - 1];
          if (field === 'slug' || field === 'name' || field === 'admin_email')
            mapped[field] = fe.msg;
        }
        setErrors(mapped);
        setFailure(err);
      } else {
        setFailure(err);
      }
    } finally {
      setBusy(false);
    }
  }

  const id = (f: string) => `${uid}-${f}`;
  return (
    <form onSubmit={(e) => void submit(e)} className="card space-y-4" noValidate>
      <h2 className="text-base font-semibold text-slate-900">Create {noun}</h2>
      {forbidden && (
        <p
          role="alert"
          className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900"
        >
          You do not have permission to create a {noun}. Ask an administrator for access.
        </p>
      )}
      <div>
        <label htmlFor={id('name')} className="label">
          Name
        </label>
        <input
          id={id('name')}
          className="input"
          value={name}
          maxLength={200}
          aria-invalid={errors.name ? true : undefined}
          aria-describedby={errors.name ? id('name-err') : undefined}
          onChange={(e) => onNameChange(e.target.value)}
        />
        {errors.name && (
          <p id={id('name-err')} className="mt-1 text-sm text-red-700">
            {errors.name}
          </p>
        )}
      </div>
      <div>
        <label htmlFor={id('slug')} className="label">
          Slug
        </label>
        <input
          id={id('slug')}
          className="input font-mono"
          value={slug}
          aria-invalid={errors.slug ? true : undefined}
          aria-describedby={`${id('slug-help')}${errors.slug ? ` ${id('slug-err')}` : ''}`}
          onChange={(e) => {
            setSlugTouched(true);
            setSlug(e.target.value);
          }}
        />
        <p id={id('slug-help')} className="mt-1 text-xs text-slate-600">
          Used in URLs. Lowercase letters, digits and hyphens.
        </p>
        {errors.slug && (
          <p id={id('slug-err')} className="mt-1 text-sm text-red-700">
            {errors.slug}
          </p>
        )}
      </div>
      {withDescription && (
        <div>
          <label htmlFor={id('desc')} className="label">
            Description (optional)
          </label>
          <textarea
            id={id('desc')}
            className="input"
            rows={2}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
      )}
      {withAdminEmail && (
        <div>
          <label htmlFor={id('admin')} className="label">
            First administrator email (optional)
          </label>
          <input
            id={id('admin')}
            type="email"
            className="input"
            value={adminEmail}
            aria-invalid={errors.admin_email ? true : undefined}
            aria-describedby={errors.admin_email ? id('admin-err') : undefined}
            onChange={(e) => setAdminEmail(e.target.value)}
          />
          {errors.admin_email && (
            <p id={id('admin-err')} className="mt-1 text-sm text-red-700">
              {errors.admin_email}
            </p>
          )}
        </div>
      )}
      {failure !== null && <ErrorMessage error={failure} title={`Could not create ${noun}`} />}
      <button type="submit" className="btn btn-primary" disabled={busy || forbidden}>
        {busy ? 'Creating...' : `Create ${noun}`}
      </button>
    </form>
  );
}
