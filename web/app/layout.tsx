import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Solstice Airdrop · Hanyon Analytics',
  description: 'On-chain view of Solstice airdrop fee-payers, cohorts, and Exponent YT activity.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="bg-ink-950 text-ink-100 antialiased">
        {children}
      </body>
    </html>
  );
}
