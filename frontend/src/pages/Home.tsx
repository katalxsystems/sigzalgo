import { LogIn } from 'lucide-react'
import { Link } from 'react-router'
import { Button } from '@/components/ui/button'

export default function Home() {
  return (
    <div className="min-h-screen flex items-center justify-center">
      <Button size="lg" asChild>
        <Link to="/login">
          <LogIn className="mr-2 h-5 w-5" />
          Login
        </Link>
      </Button>
    </div>
  )
}
